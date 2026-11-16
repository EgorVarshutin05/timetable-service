"""FR-15: ISO calendar, BR-06 arithmetic and API ordering without SQL/network."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_workload_service
from app.domain.lesson import LessonStatus
from app.domain.people import InstructorProfile
from app.domain.scheduling import InvalidLesson, ScheduledLesson
from app.domain.workload import parse_week, summarize_workload, week_bounds
from app.errors import ApiError
from app.main import create_app
from app.services import workload as workload_module
from app.services.workload import InstructorSource, WorkloadService
from tests.unit.scheduling_data import C1, G1, I1, I2, PEOPLE
from tests.unit.test_lesson_commands_api import CONTRACT

PROFILE = PEOPLE.instructor
DATES = [
    "2026-11-30",
    "2026-12-01",
    "2026-12-02",
    "2026-12-03",
    "2026-12-04",
    "2026-12-05",
    "2026-12-06",
]


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def slot(
    start: str, end: str, status: LessonStatus = LessonStatus.PLANNED, instructor: UUID = I1
) -> ScheduledLesson:
    return ScheduledLesson(uuid4(), G1, instructor, C1, moment(start), moment(end), status)


@pytest.mark.parametrize(
    ("value", "monday", "sunday"),
    [
        ("2026-W49", "2026-11-30", "2026-12-06"),
        ("2020-W53", "2020-12-28", "2021-01-03"),
        ("2026-W53", "2026-12-28", "2027-01-03"),
        ("2019-W01", "2018-12-31", "2019-01-06"),
        ("2021-W52", "2021-12-27", "2022-01-02"),
    ],
)
def test_existing_iso_weeks(value: str, monday: str, sunday: str) -> None:
    week = parse_week(value)
    assert week.value == value
    assert len(week.days) == 7
    assert week.days[0] == date.fromisoformat(monday)
    assert week.days[-1] == date.fromisoformat(sunday)
    assert week.days[0].isoweekday() == 1


BAD_WEEKS = [
    "",
    "2026-W00",
    "2026-W54",
    "2021-W53",
    "2026-W1",
    "2026-W049",
    "2026-w49",
    "2026-W49x",
    "2026-W49\n",
    "2026-W49 ",
    "0000-W01",
    "9999-W52",
    "202649",
    "2026-W+1",
    "２０２６-W４９",
]


@pytest.mark.parametrize("value", BAD_WEEKS)
def test_invalid_iso_weeks(value: str) -> None:
    with pytest.raises(InvalidLesson) as error:
        parse_week(value)
    assert error.value.issue.code == "VALIDATION_ERROR"
    assert set(error.value.issue.details["fields"]) == {"week"}


@pytest.mark.parametrize(
    ("week", "hours"), [("2026-W13", 167), ("2026-W43", 169), ("2026-W49", 168)]
)
def test_week_bounds_follow_local_midnights_and_dst(week: str, hours: int) -> None:
    start, end = week_bounds(parse_week(week), "Europe/Berlin")
    assert start.tzinfo == UTC
    assert end - start == timedelta(hours=hours)


def test_empty_week_has_seven_ordered_days() -> None:
    report = summarize_workload(PROFILE, parse_week("2026-W49"), [])
    assert report.week == "2026-W49"
    assert [day.date.isoformat() for day in report.days] == DATES
    assert all(day.hours == Decimal(0) and not day.exceeded for day in report.days)


def test_local_midnight_splits_hours() -> None:
    lesson = slot("2026-11-30T18:30:00Z", "2026-11-30T20:00:00Z")
    report = summarize_workload(PROFILE, parse_week("2026-W49"), [lesson])
    assert [day.hours for day in report.days] == [Decimal("0.5"), Decimal(1), 0, 0, 0, 0, 0]


def test_week_edges_clip_and_exact_boundaries_are_excluded() -> None:
    lessons = [
        slot("2026-11-29T18:00:00Z", "2026-11-29T20:00:00Z"),
        slot("2026-12-06T18:30:00Z", "2026-12-06T20:00:00Z"),
        slot("2026-11-29T17:00:00Z", "2026-11-29T19:00:00Z"),
        slot("2026-12-06T19:00:00Z", "2026-12-06T20:00:00Z"),
    ]
    report = summarize_workload(PROFILE, parse_week("2026-W49"), lessons)
    assert [day.hours for day in report.days] == [1, 0, 0, 0, 0, 0, Decimal("0.5")]


@pytest.mark.parametrize("status", list(LessonStatus))
def test_only_active_statuses_contribute(status: LessonStatus) -> None:
    lesson = slot("2026-11-30T07:00:00Z", "2026-11-30T09:00:00Z", status)
    report = summarize_workload(PROFILE, parse_week("2026-W49"), [lesson])
    expected = 0 if status in {LessonStatus.CANCELLED, LessonStatus.RESCHEDULED} else 2
    assert report.days[0].hours == expected


def test_other_instructors_do_not_contribute() -> None:
    report = summarize_workload(
        PROFILE,
        parse_week("2026-W49"),
        [
            slot("2026-11-30T07:00:00Z", "2026-11-30T09:00:00Z", instructor=I2),
        ],
    )
    assert all(day.hours == 0 for day in report.days)


@pytest.mark.parametrize(
    ("end", "exceeded"), [("2026-11-30T13:00:00Z", False), ("2026-11-30T13:00:00.000001Z", True)]
)
def test_limit_is_strict_and_compared_before_float_conversion(end: str, exceeded: bool) -> None:
    report = summarize_workload(
        PROFILE,
        parse_week("2026-W49"),
        [
            slot("2026-11-30T07:00:00Z", end),
        ],
    )
    assert report.days[0].exceeded == exceeded


@pytest.mark.parametrize(
    ("week", "start", "end", "expected"),
    [
        ("2026-W13", "2026-03-29T00:30:00Z", "2026-03-29T02:30:00Z", 2),
        ("2026-W43", "2026-10-24T23:30:00Z", "2026-10-25T02:30:00Z", 3),
    ],
)
def test_dst_counts_elapsed_utc_hours(week: str, start: str, end: str, expected: int) -> None:
    profile = replace(PROFILE, timezone="Europe/Berlin", max_hours_per_day=Decimal(2))
    report = summarize_workload(profile, parse_week(week), [slot(start, end)])
    assert report.days[6].hours == expected
    assert report.days[6].exceeded == (expected > 2)
    assert all(day.hours == 0 for day in report.days[:6])


@dataclass
class WorkloadApp:
    client: httpx.AsyncClient
    factory: MagicMock
    people: AsyncMock
    query: AsyncMock
    session: AsyncMock


@pytest.fixture
async def workload_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkloadApp]:
    factory = MagicMock(spec=async_sessionmaker)
    session = AsyncMock(spec=AsyncSession)
    factory.return_value.__aenter__.return_value = session
    people = AsyncMock(spec=InstructorSource)
    people.get_instructor.return_value = PROFILE
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(workload_module, "get_week_lessons", query)
    service = WorkloadService(
        cast(async_sessionmaker[AsyncSession], factory), cast(InstructorSource, people)
    )
    app = create_app()
    app.dependency_overrides[get_workload_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield WorkloadApp(client, factory, people, query, session)


@pytest.mark.parametrize("week", [None, *BAD_WEEKS])
async def test_invalid_or_missing_week_precedes_all_io(
    workload_app: WorkloadApp, week: str | None
) -> None:
    response = await workload_app.client.get(
        f"/instructors/{I1}/workload", params={} if week is None else {"week": week}
    )
    details = CONTRACT.assert_error(response, 422, "VALIDATION_ERROR")
    assert "week" in details["fields"]
    workload_app.factory.assert_not_called()
    workload_app.people.get_instructor.assert_not_awaited()
    workload_app.query.assert_not_awaited()


async def test_invalid_path_uuid_is_400_without_io(workload_app: WorkloadApp) -> None:
    response = await workload_app.client.get(
        "/instructors/not-uuid/workload", params={"week": "2026-W49"}
    )
    CONTRACT.assert_error(response, 400, "BAD_REQUEST")
    workload_app.factory.assert_not_called()
    workload_app.people.get_instructor.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "code", "expected_status", "expected_code"),
    [
        (422, "INSTRUCTOR_NOT_FOUND", 404, "NOT_FOUND"),
        (502, "UPSTREAM_UNAVAILABLE", 502, "UPSTREAM_UNAVAILABLE"),
        (504, "UPSTREAM_TIMEOUT", 504, "UPSTREAM_TIMEOUT"),
    ],
)
async def test_upstream_error_context_and_no_database(
    workload_app: WorkloadApp,
    status: int,
    code: str,
    expected_status: int,
    expected_code: str,
) -> None:
    workload_app.people.get_instructor.side_effect = ApiError(status, code, "Stub failed")
    response = await workload_app.client.get(
        f"/instructors/{I1}/workload", params={"week": "2026-W49"}
    )
    CONTRACT.assert_error(response, expected_status, expected_code)
    workload_app.people.get_instructor.assert_awaited_once_with(I1)
    workload_app.factory.assert_not_called()


async def test_workload_response_profile_first_and_read_only(workload_app: WorkloadApp) -> None:
    async def profile(instructor_id: UUID) -> InstructorProfile:
        assert instructor_id == I1
        workload_app.factory.assert_not_called()
        return PROFILE

    workload_app.people.get_instructor.side_effect = profile
    response = await workload_app.client.get(
        f"/instructors/{I1}/workload", params={"week": "2026-W49"}
    )
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "Workload")
    assert response.json() == {
        "instructor_id": str(I1),
        "week": "2026-W49",
        "max_hours_per_day": 6.0,
        "days": [{"date": day, "hours": 0.0, "exceeded": False} for day in DATES],
    }
    workload_app.query.assert_awaited_once_with(
        workload_app.session, I1, moment("2026-11-29T19:00:00Z"), moment("2026-12-06T19:00:00Z")
    )
    workload_app.people.get_instructor.assert_awaited_once()
    workload_app.session.commit.assert_not_awaited()
