"""FR-15: реальный SQL, seed, серии WP-08 и локальные календарные границы."""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import update

from app.clients.people import PeopleClient
from app.domain.lesson import LessonStatus
from app.models import Lesson
from app.settings import Settings
from tests.unit.event_helpers import CapturingPublisher
from tests.unit.scheduling_data import I1, L1
from tests.unit.test_instructor_workload import DATES, moment
from tests.unit.test_lesson_commands_api import BODY as SINGLE_BODY
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_listing_and_patch import row
from tests.unit.test_lesson_series import BODY as SERIES_BODY
from tests.unit.test_people_client import INSTRUCTORS, CountingTransport, Failure
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database
from tests.unit.test_wp03_postgres import RunningApp
from tests.unit.test_wp03_postgres import running as running

BASE_HOURS = [0.0, 1.5, 0.0, 1.0, 0.0, 0.0, 0.0]


async def install_transport(
    running: RunningApp,
    *,
    failures: dict[str, Failure] | None = None,
    profile: dict[str, Any] | None = None,
) -> CountingTransport:
    assert running.context.people_client is not None
    await running.context.people_client.aclose()
    overrides: dict[str, object] = {} if profile is None else {f"/instructors/{I1}": profile}
    transport = CountingTransport(failures=failures, overrides=overrides)
    running.context.people_client = PeopleClient(Settings(), transport=transport)
    return transport


async def get_report(
    running: RunningApp, instructor: UUID = I1, week: str = "2026-W49"
) -> dict[str, Any]:
    response = await running.client.get(
        f"/instructors/{instructor}/workload", params={"week": week}
    )
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "Workload")
    return dict(response.json())


def no_events(running: RunningApp) -> None:
    publisher = running.context.event_publisher
    assert isinstance(publisher, CapturingPublisher)
    assert publisher.events == []


async def test_seed_i1_week49_and_only_profile_request(running: RunningApp) -> None:
    transport = await install_transport(running)
    result = await get_report(running)
    assert result == {
        "instructor_id": str(I1),
        "week": "2026-W49",
        "max_hours_per_day": 6.0,
        "days": [
            {"date": day, "hours": hours, "exceeded": False}
            for day, hours in zip(DATES, BASE_HOURS, strict=True)
        ],
    }
    assert transport.paths == [f"/instructors/{I1}"]
    assert transport.max_in_flight == 1
    no_events(running)


@pytest.mark.parametrize("status", list(LessonStatus))
async def test_all_active_statuses_include_completed(
    running: RunningApp, database: Database, status: LessonStatus
) -> None:
    # L5 — среда, исходно CANCELLED. Изменение статуса изолирует SQL/domain фильтр.
    async with database.factory() as session, session.begin():
        await session.execute(
            update(Lesson).where(Lesson.id == UUID(int=L1.int + 4)).values(status=status)
        )
    result = await get_report(running)
    expected = 0.0 if status in {LessonStatus.CANCELLED, LessonStatus.RESCHEDULED} else 1.5
    assert result["days"][2]["hours"] == expected
    assert result["days"][1]["hours"] == 1.5
    assert result["days"][3]["hours"] == 1.0
    no_events(running)


@pytest.mark.parametrize(
    ("start", "end", "index", "hours"),
    [
        ("2026-11-29T18:00:00Z", "2026-11-29T20:00:00Z", 0, 1.0),
        ("2026-12-06T18:30:00Z", "2026-12-06T20:00:00Z", 6, 0.5),
        ("2026-11-29T17:00:00Z", "2026-11-29T19:00:00Z", 0, 0.0),
        ("2026-12-06T19:00:00Z", "2026-12-06T20:00:00Z", 6, 0.0),
    ],
)
async def test_sql_overlap_and_week_clipping(
    running: RunningApp, database: Database, start: str, end: str, index: int, hours: float
) -> None:
    lesson = row()
    lesson.id = uuid4()
    lesson.starts_at, lesson.ends_at = moment(start), moment(end)
    async with database.factory() as session, session.begin():
        session.add(lesson)
    result = await get_report(running)
    expected = list(BASE_HOURS)
    expected[index] = hours
    assert [day["hours"] for day in result["days"]] == expected


async def test_local_midnight_is_split_by_the_br06_function(
    running: RunningApp, database: Database
) -> None:
    lesson = row()
    lesson.id = uuid4()
    lesson.starts_at = moment("2026-11-30T18:30:00Z")
    lesson.ends_at = moment("2026-11-30T20:00:00Z")
    async with database.factory() as session, session.begin():
        session.add(lesson)
    result = await get_report(running)
    assert [day["hours"] for day in result["days"]] == [0.5, 2.5, 0, 1, 0, 0, 0]


async def test_series_members_contribute_to_workload(running: RunningApp) -> None:
    series = await running.client.post(
        "/lesson-series",
        json=SERIES_BODY
        | {
            "weekdays": ["MON", "WED", "FRI"],
            "first_starts_at": "2026-11-30T07:00:00Z",
            "count": 3,
        },
    )
    assert series.status_code == 201, series.text
    transport = await install_transport(running)
    result = await get_report(running)
    assert [day["hours"] for day in result["days"]] == [1.5, 1.5, 1.5, 1, 1.5, 0, 0]
    assert transport.paths == [f"/instructors/{I1}"]
    no_events(running)


async def test_exceeded_matches_br06_at_and_above_the_limit(running: RunningApp) -> None:
    for start, end in [("07:00:00", "11:00:00"), ("11:00:00", "13:00:00")]:
        created = await running.client.post(
            "/lessons",
            json=SINGLE_BODY
            | {
                "starts_at": f"2026-11-30T{start}Z",
                "ends_at": f"2026-11-30T{end}Z",
            },
        )
        assert created.status_code == 201, created.text
    exact = await get_report(running)
    assert exact["days"][0] == {"date": "2026-11-30", "hours": 6.0, "exceeded": False}
    created = await running.client.post(
        "/lessons",
        json=SINGLE_BODY
        | {
            "starts_at": "2026-11-30T13:00:00Z",
            "ends_at": "2026-11-30T13:30:00Z",
        },
    )
    assert created.status_code == 201
    warning = next(w for w in created.json()["warnings"] if w["code"] == "WORKLOAD_SOFT_LIMIT")
    report = await get_report(running)
    assert report["days"][0] == {"date": "2026-11-30", "hours": 6.5, "exceeded": True}
    assert report["days"][0]["hours"] == warning["details"]["planned_hours"]
    assert report["max_hours_per_day"] == warning["details"]["max_hours"]
    no_events(running)


@pytest.mark.parametrize(
    ("week", "start", "end", "hours"),
    [
        ("2026-W13", "2026-03-29T00:30:00Z", "2026-03-29T02:30:00Z", 2.0),
        ("2026-W43", "2026-10-24T23:30:00Z", "2026-10-25T02:30:00Z", 3.0),
    ],
)
async def test_dst_week_with_real_sql(
    running: RunningApp, database: Database, week: str, start: str, end: str, hours: float
) -> None:
    profile = dict(INSTRUCTORS[str(I1)]) | {"timezone": "Europe/Berlin", "max_hours_per_day": 2}
    await install_transport(running, profile=profile)
    lesson = row(LessonStatus.COMPLETED)
    lesson.id = uuid4()
    lesson.starts_at, lesson.ends_at = moment(start), moment(end)
    async with database.factory() as session, session.begin():
        session.add(lesson)
    result = await get_report(running, week=week)
    assert result["days"][6]["hours"] == hours
    assert result["days"][6]["exceeded"] == (hours > 2)
    assert all(day["hours"] == 0 for day in result["days"][:6])


async def test_i6_does_not_request_failing_availability(running: RunningApp) -> None:
    instructor = UUID(int=I1.int + 5)
    transport = await install_transport(running)
    result = await get_report(running, instructor)
    assert all(day["hours"] == 0 and not day["exceeded"] for day in result["days"])
    assert transport.paths == [f"/instructors/{instructor}"]


@pytest.mark.parametrize(
    ("suffix", "failure", "status", "code"),
    [
        (99, None, 404, "NOT_FOUND"),
        (4, 500, 502, "UPSTREAM_UNAVAILABLE"),
        (5, httpx.ReadTimeout, 504, "UPSTREAM_TIMEOUT"),
    ],
)
async def test_real_people_client_maps_path_absence_and_failures(
    running: RunningApp, suffix: int, failure: Failure | None, status: int, code: str
) -> None:
    instructor = UUID(int=I1.int + suffix - 1)
    path = f"/instructors/{instructor}"
    transport = await install_transport(
        running, failures={} if failure is None else {path: failure}
    )
    response = await running.client.get(f"{path}/workload", params={"week": "2026-W49"})
    CONTRACT.assert_error(response, status, code)
    assert transport.paths == [path]
    no_events(running)
