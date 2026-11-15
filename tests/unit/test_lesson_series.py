"""FR-14: чистая UTC-генерация, ранние ошибки и API без сети/БД."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import AdjustableClock
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.scheduling import Assessment, InvalidLesson, ScheduledLesson, workload_warnings
from app.domain.series import SeriesPlan, Weekday, generate_series
from app.errors import ApiError
from app.models import Group, Lesson
from app.repositories import scheduling
from app.services import lessons
from tests.unit.scheduling_data import C1, G1, I1, L1, PEOPLE
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_commands_api import command_client as command_client

PLAN = SeriesPlan(
    G1, I1, C1, LessonFormat.IN_PERSON, (Weekday.THU,), 3, datetime(2026, 12, 10, 7, tzinfo=UTC), 90
)
BODY: dict[str, Any] = {
    "group_id": str(G1),
    "instructor_id": str(I1),
    "classroom_id": str(C1),
    "format": "IN_PERSON",
    "weekdays": ["THU"],
    "count": 3,
    "first_starts_at": "2026-12-10T07:00:00Z",
    "duration_minutes": 90,
}


@pytest.mark.parametrize(
    ("weekdays", "expected"),
    [
        ((Weekday.MON,), ["2026-12-14", "2026-12-21", "2026-12-28"]),
        ((Weekday.THU,), ["2026-12-10", "2026-12-17", "2026-12-24"]),
        ((Weekday.SUN, Weekday.FRI), ["2026-12-11", "2026-12-13", "2026-12-18"]),
    ],
)
def test_chronological_weekday_generation(
    weekdays: tuple[Weekday, ...], expected: list[str]
) -> None:
    drafts = generate_series(replace(PLAN, weekdays=weekdays))
    assert [d.starts_at.date().isoformat() for d in drafts] == expected
    assert all(d.starts_at.hour == 7 and d.starts_at.tzinfo == UTC for d in drafts)
    assert all(d.ends_at - d.starts_at == timedelta(minutes=90) for d in drafts)


@pytest.mark.parametrize(
    ("day", "first_date"), [(Weekday.MON, "2026-11-30"), (Weekday.TUE, "2026-12-01")]
)
def test_weekday_uses_utc_not_the_input_offset(day: Weekday, first_date: str) -> None:
    first = datetime.fromisoformat("2026-12-01T01:00:00+05:00")
    drafts = generate_series(replace(PLAN, weekdays=(day,), first_starts_at=first))
    assert drafts[0].starts_at.date().isoformat() == first_date
    assert all(d.starts_at.hour == 20 for d in drafts)


def test_leap_day_and_year_rollover() -> None:
    leap = generate_series(
        replace(
            PLAN,
            weekdays=tuple(Weekday),
            count=3,
            first_starts_at=datetime(2028, 2, 28, 7, tzinfo=UTC),
        )
    )
    assert [d.starts_at.day for d in leap] == [28, 29, 1]
    year = generate_series(
        replace(
            PLAN,
            weekdays=tuple(Weekday),
            count=2,
            first_starts_at=datetime(2026, 12, 31, 7, tzinfo=UTC),
        )
    )
    assert year[1].starts_at == datetime(2027, 1, 1, 7, tzinfo=UTC)


@pytest.mark.parametrize("count", [1, 50])
@pytest.mark.parametrize("duration", [30, 480])
def test_generation_inclusive_boundaries(count: int, duration: int) -> None:
    drafts = generate_series(replace(PLAN, count=count, duration_minutes=duration))
    assert len(drafts) == count
    assert all(d.ends_at - d.starts_at == timedelta(minutes=duration) for d in drafts)


@pytest.mark.parametrize(
    "changes",
    [
        {"count": 0},
        {"count": 51},
        {"duration_minutes": 29},
        {"duration_minutes": 481},
        {"weekdays": ()},
        {"weekdays": (Weekday.MON, Weekday.MON)},
        {"first_starts_at": datetime(2026, 12, 10, 7)},
        {"first_starts_at": datetime(9999, 12, 31, 23, 50, tzinfo=UTC), "weekdays": tuple(Weekday)},
    ],
)
def test_invalid_series_plan_is_pure(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidLesson) as error:
        generate_series(replace(PLAN, **changes))
    assert error.value.issue.code == "VALIDATION_ERROR"
    assert error.value.issue.details["fields"]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"classroom_id": None}, "CLASSROOM_REQUIRED"),
        ({"format": LessonFormat.ONLINE}, "VALIDATION_ERROR"),
    ],
)
def test_early_rules_identify_first_occurrence(changes: dict[str, Any], code: str) -> None:
    with pytest.raises(InvalidLesson) as error:
        generate_series(replace(PLAN, **changes))
    assert error.value.issue.code == code
    assert error.value.issue.details["occurrence_index"] == 0


def test_workload_accumulates_previous_occurrence_across_local_midnight() -> None:
    drafts = generate_series(
        replace(
            PLAN,
            weekdays=tuple(Weekday),
            count=2,
            first_starts_at=datetime(2026, 12, 10, 17, tzinfo=UTC),
            duration_minutes=480,
        )
    )
    assert workload_warnings(drafts[0], [], PEOPLE) == ()
    previous = ScheduledLesson(
        L1, G1, I1, C1, drafts[0].starts_at, drafts[0].ends_at, LessonStatus.PLANNED
    )
    warnings = workload_warnings(drafts[1], [previous], PEOPLE)
    assert len(warnings) == 1
    assert warnings[0].details == {"date": "2026-12-11", "max_hours": 6.0, "planned_hours": 8.0}


def test_series_column_is_nullable_uuid_with_index() -> None:
    table = Lesson.metadata.tables["lessons"]
    column = table.c.series_id
    assert column.nullable
    assert column.type.python_type is UUID
    assert any(index.name == "ix_lessons_series_id" for index in table.indexes)


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"count": 0}, 422, "VALIDATION_ERROR"),
        ({"count": 51}, 422, "VALIDATION_ERROR"),
        ({"duration_minutes": 29}, 422, "VALIDATION_ERROR"),
        ({"duration_minutes": 481}, 422, "VALIDATION_ERROR"),
        ({"weekdays": []}, 422, "VALIDATION_ERROR"),
        ({"weekdays": ["MON", "MON"]}, 422, "VALIDATION_ERROR"),
        ({"weekdays": ["mon"]}, 422, "VALIDATION_ERROR"),
        ({"first_starts_at": "2026-12-10T07:00:00"}, 422, "VALIDATION_ERROR"),
        ({"first_starts_at": 1796886000}, 400, "BAD_REQUEST"),
        ({"count": "2"}, 400, "BAD_REQUEST"),
        ({"count": True}, 400, "BAD_REQUEST"),
        ({"duration_minutes": 30.5}, 400, "BAD_REQUEST"),
        ({"weekdays": "MON"}, 400, "BAD_REQUEST"),
        ({"weekdays": [1]}, 400, "BAD_REQUEST"),
        ({"instructor_id": "bad-uuid"}, 400, "BAD_REQUEST"),
        ({"format": {}}, 400, "BAD_REQUEST"),
        ({"classroom_id": None}, 422, "CLASSROOM_REQUIRED"),
        ({"format": "ONLINE"}, 422, "VALIDATION_ERROR"),
        ({"series_id": str(L1)}, 422, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_series_never_calls_database_or_people(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    changes: dict[str, Any],
    status: int,
    code: str,
) -> None:
    client, factory, people = command_client
    response = await client.post("/lesson-series", json=BODY | changes)
    CONTRACT.assert_error(response, status, code)
    factory.assert_not_called()
    people.get_lesson_people.assert_not_awaited()


async def test_group_absence_precedes_upstream(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, people = command_client
    monkeypatch.setattr(scheduling, "get_group", AsyncMock(return_value=None))
    response = await client.post("/lesson-series", json=BODY)
    assert CONTRACT.assert_error(response, 422, "GROUP_NOT_FOUND") == {
        "group_id": str(G1),
        "occurrence_index": 0,
    }
    assert factory.call_count == 1
    people.get_lesson_people.assert_not_awaited()


@pytest.fixture
async def series_client(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> tuple[httpx.AsyncClient, AsyncMock, AsyncMock, AsyncMock]:
    client, factory, people = command_client
    session = AsyncMock(spec=AsyncSession)
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(
        scheduling, "get_group", AsyncMock(return_value=Group(id=G1, name="G", size=12))
    )
    monkeypatch.setattr(scheduling, "lock_resources", AsyncMock())
    assess = AsyncMock(return_value=Assessment((), ()))
    monkeypatch.setattr(lessons, "assess", assess)
    people.get_lesson_people.return_value = PEOPLE
    monkeypatch.setattr(AdjustableClock, "now", lambda _: datetime(2026, 11, 30, 3, tzinfo=UTC))
    return client, session, people, assess


async def test_series_api_success_and_single_people_lookup(
    series_client: tuple[httpx.AsyncClient, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    client, session, people, assess = series_client
    response = await client.post("/lesson-series", json=BODY)
    assert response.status_code == 201, response.text
    CONTRACT.assert_schema(response.json(), "LessonSeriesResult")
    result = response.json()
    assert UUID(result["series_id"]).version == 4
    assert len(result["items"]) == 3
    assert len({item["lesson"]["id"] for item in result["items"]}) == 3
    assert all(item["lesson"]["series_id"] == result["series_id"] for item in result["items"])
    assert all(item["lesson"]["status"] == "PLANNED" for item in result["items"])
    assert all(item["lesson"]["created_at"] == "2026-11-30T03:00:00Z" for item in result["items"])
    people.get_lesson_people.assert_awaited_once()
    assert assess.await_count == 3
    assert session.flush.await_count == 3
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("changes", [{"count": 2.0}, {"duration_minutes": 30.0}])
async def test_json_schema_integral_numbers_are_accepted(
    series_client: tuple[httpx.AsyncClient, AsyncMock, AsyncMock, AsyncMock],
    changes: dict[str, float],
) -> None:
    client, _, _, _ = series_client
    response = await client.post("/lesson-series", json=BODY | changes)
    assert response.status_code == 201, response.text
    assert len(response.json()["items"]) == int(changes.get("count", 3))
    CONTRACT.assert_schema(response.json(), "LessonSeriesResult")


@pytest.mark.parametrize(
    ("index", "code", "details"),
    [
        (0, "CLASSROOM_TOO_SMALL", {"capacity": 10, "group_size": 12}),
        (2, "SCHEDULE_CONFLICT", {"conflicts": [{"resource": "instructor", "lesson_id": str(L1)}]}),
    ],
)
async def test_first_hard_violation_adds_index_and_rolls_back(
    series_client: tuple[httpx.AsyncClient, AsyncMock, AsyncMock, AsyncMock],
    index: int,
    code: str,
    details: dict[str, Any],
) -> None:
    client, session, _, assess = series_client
    assess.side_effect = [Assessment((), ())] * index + [ApiError(409, code, "Нарушение", details)]
    response = await client.post("/lesson-series", json=BODY)
    assert CONTRACT.assert_error(response, 409, code) == details | {"occurrence_index": index}
    assert session.flush.await_count == index
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "INSTRUCTOR_NOT_FOUND"),
        (422, "CLASSROOM_NOT_FOUND"),
        (502, "UPSTREAM_UNAVAILABLE"),
        (504, "UPSTREAM_TIMEOUT"),
    ],
)
async def test_series_preserves_upstream_errors(
    series_client: tuple[httpx.AsyncClient, AsyncMock, AsyncMock, AsyncMock], status: int, code: str
) -> None:
    client, session, people, assess = series_client
    people.get_lesson_people.side_effect = ApiError(status, code, "Upstream failed")
    response = await client.post("/lesson-series", json=BODY)
    assert CONTRACT.assert_error(response, status, code)["occurrence_index"] == 0
    assess.assert_not_awaited()
    session.commit.assert_not_awaited()
