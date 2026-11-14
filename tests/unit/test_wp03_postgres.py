"""HTTP + реальный PostgreSQL: FR-01/02, данные E2E-01/02 и конкурентные INSERT."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text

from app import main
from app.clients.people import PeopleClient
from app.context import AppContext
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.scheduling import DraftLesson, ScheduledLesson
from app.models import Lesson
from app.repositories import scheduling
from app.services.lessons import assess
from app.settings import Settings
from tests.e2e.public import test_e2e_01_create_lesson as public_create
from tests.e2e.report import Recorder, ScenarioRecord
from tests.unit.event_helpers import CapturingPublisher
from tests.unit.scheduling_data import C1, C2, DRAFT, G1, G2, I1, I2, L1
from tests.unit.test_lesson_commands_api import BODY, CONTRACT
from tests.unit.test_people_client import CountingTransport
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database


@dataclass
class RunningApp:
    client: httpx.AsyncClient
    context: AppContext


@pytest.fixture
async def running(database: Database, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RunningApp]:
    monkeypatch.setattr(main, "make_engine", lambda _: database.engine)
    context = main.build_context(Settings(test_mode=True))
    context.event_publisher = CapturingPublisher()
    assert context.people_client is not None
    await context.people_client.aclose()
    context.people_client = PeopleClient(Settings(), transport=CountingTransport(delay=0.001))
    context.clock.freeze(datetime(2026, 11, 30, 3, tzinfo=UTC))
    app = main.create_app()
    app.state.context = context
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://service"
        ) as client:
            yield RunningApp(client, context)
    finally:
        await context.people_client.aclose()
        if context.broker_client is not None:
            await context.broker_client.aclose()


async def lesson_count(database: Database) -> int:
    async with database.factory() as session:
        count = await session.scalar(select(func.count()).select_from(Lesson))
        assert count is not None
        return count


async def test_public_e2e01_source_against_postgres(running: RunningApp) -> None:
    """Вызывается исходная функция E2E-01 с PostgreSQL и ASGI API, без подмены её assertions."""
    await public_create.test_create_in_person_lesson_without_violations(
        running.client,
        {"G1": str(G1), "I1": str(I1), "C1": str(C1)},
        CONTRACT.fork(),
        Recorder(ScenarioRecord("E2E-01", "Создание", True)),
    )


async def test_e2e01_created_values_and_utc_offset(running: RunningApp) -> None:
    body = BODY | {"starts_at": "2026-12-02T12:00:00+05:00", "ends_at": "2026-12-02T13:30:00+05:00"}
    response = await running.client.post("/lessons", json=body)
    assert response.status_code == 201, response.text
    CONTRACT.assert_schema(response.json(), "LessonWithWarnings")
    lesson = response.json()["lesson"]
    assert lesson["created_at"] == lesson["updated_at"] == "2026-11-30T03:00:00Z"
    assert lesson["starts_at"] == "2026-12-02T07:00:00Z"
    assert UUID(lesson["id"]).version == 4
    assert response.json()["warnings"] == []


async def test_e2e02_rejection_and_no_insert(running: RunningApp, database: Database) -> None:
    body = BODY | {
        "group_id": "9a000000-0000-4000-8000-000000000003",
        "format": "ONLINE",
        "classroom_id": None,
        "starts_at": "2026-12-01T06:00:00Z",
        "ends_at": "2026-12-01T07:00:00Z",
    }
    response = await running.client.post("/lessons", json=body)
    details = CONTRACT.assert_error(response, 409, "SCHEDULE_CONFLICT")
    assert details["conflicts"] == [
        {
            "resource": "instructor",
            "lesson_id": str(L1),
            "overlap": {"starts_at": "2026-12-01T06:00:00Z", "ends_at": "2026-12-01T06:30:00Z"},
        }
    ]
    assert await lesson_count(database) == 8
    # Список FR-03 — WP-04; тот же инвариант отсутствия вставки проверяется через SQL.
    async with database.factory() as session:
        rows = (
            await session.scalars(
                select(Lesson.id).where(
                    Lesson.instructor_id == I1,
                    Lesson.starts_at >= datetime(2026, 12, 1, tzinfo=UTC),
                    Lesson.starts_at < datetime(2026, 12, 2, tzinfo=UTC),
                )
            )
        ).all()
    assert list(rows) == [L1]


async def test_validate_collects_capacity_and_conflict_without_write(
    running: RunningApp, database: Database
) -> None:
    body = BODY | {
        "classroom_id": str(C2),
        "starts_at": "2026-12-01T05:00:00Z",
        "ends_at": "2026-12-01T06:00:00Z",
    }
    validated = await running.client.post("/lessons/validate", json=body)
    assert validated.status_code == 200, validated.text
    CONTRACT.assert_schema(validated.json(), "ValidationResult")
    assert validated.json()["ok"] is False
    assert [issue["code"] for issue in validated.json()["violations"]] == [
        "CLASSROOM_TOO_SMALL",
        "SCHEDULE_CONFLICT",
    ]
    created = await running.client.post("/lessons", json=body)
    assert CONTRACT.assert_error(created, 409, "CLASSROOM_TOO_SMALL") == {
        "capacity": 10,
        "group_size": 12,
    }
    assert await lesson_count(database) == 8
    # Ошибка освободила xact locks: следующий запрос на те же ресурсы не зависает.
    async with asyncio.timeout(2):
        assert (await running.client.post("/lessons", json=BODY)).status_code == 201


async def test_validate_success_does_not_reserve_or_insert(
    running: RunningApp, database: Database
) -> None:
    for _ in range(2):
        response = await running.client.post("/lessons/validate", json=BODY)
        assert response.status_code == 200
        assert response.json() == {"ok": True, "violations": [], "warnings": []}
    assert await lesson_count(database) == 8
    assert (await running.client.post("/lessons", json=BODY)).status_code == 201
    assert await lesson_count(database) == 9


async def test_soft_warnings_do_not_block_past_lesson(running: RunningApp) -> None:
    body = BODY | {
        "group_id": "9a000000-0000-4000-8000-000000000003",
        "instructor_id": "1b000000-0000-4000-8000-000000000003",
        "format": "ONLINE",
        "classroom_id": None,
        "starts_at": "2026-11-27T10:30:00Z",
        "ends_at": "2026-11-27T18:30:00Z",
    }
    response = await running.client.post("/lessons", json=body)
    assert response.status_code == 201, response.text
    CONTRACT.assert_schema(response.json(), "LessonWithWarnings")
    warnings = response.json()["warnings"]
    assert [warning["code"] for warning in warnings] == [
        "WORKLOAD_SOFT_LIMIT",
        "INSTRUCTOR_UNAVAILABLE",
    ]
    assert warnings[0]["details"] == {"date": "2026-11-27", "max_hours": 8.0, "planned_hours": 9.5}


async def test_exclude_lesson_id_reuses_same_checks(
    running: RunningApp, database: Database
) -> None:
    assert running.context.people_client is not None
    people = await running.context.people_client.get_lesson_people(
        instructor_id=I1, lesson_format=DRAFT.format, classroom_id=C1
    )
    draft = DraftLesson(
        G1,
        I1,
        C1,
        DRAFT.format,
        datetime(2026, 12, 1, 5, tzinfo=UTC),
        datetime(2026, 12, 1, 6, 30, tzinfo=UTC),
    )
    async with database.factory() as session, session.begin():
        await scheduling.lock_resources(session, draft)
        report = await assess(session, draft, 12, people, exclude_lesson_id=L1)
        assert report.violations == ()


@pytest.mark.parametrize("shared", ["instructor", "classroom", "group"])
async def test_concurrent_posts_have_one_winner(
    running: RunningApp, database: Database, monkeypatch: pytest.MonkeyPatch, shared: str
) -> None:
    a: dict[str, Any] = BODY | {
        "starts_at": "2026-12-10T07:00:00Z",
        "ends_at": "2026-12-10T08:00:00Z",
    }
    b = a | {"group_id": str(G2), "instructor_id": str(I2), "classroom_id": str(C2)}
    field = {"instructor": "instructor_id", "classroom": "classroom_id", "group": "group_id"}[
        shared
    ]
    b[field] = a[field]
    # G2=25 не помещается в C2=10: для проверки гонки используем C3=20 и G3=8.
    b["group_id"] = a["group_id"] if shared == "group" else "9a000000-0000-4000-8000-000000000003"
    if shared != "classroom":
        b["classroom_id"] = "2c000000-0000-4000-8000-000000000003"
    original = scheduling.get_candidates

    async def pause_after_snapshot(
        session: Any,
        draft: DraftLesson,
        day_start: datetime,
        day_end: datetime,
        exclude_lesson_id: UUID | None = None,
    ) -> list[ScheduledLesson]:
        rows = await original(session, draft, day_start, day_end, exclude_lesson_id)
        await asyncio.sleep(0.05)  # Без locks оба запроса успели бы прочитать пустое расписание.
        return rows

    monkeypatch.setattr(scheduling, "get_candidates", pause_after_snapshot)
    async with asyncio.timeout(5):
        responses = await asyncio.gather(
            running.client.post("/lessons", json=a), running.client.post("/lessons", json=b)
        )
    assert sorted(response.status_code for response in responses) == [201, 409], [
        r.text for r in responses
    ]
    loser = next(response for response in responses if response.status_code == 409)
    CONTRACT.assert_error(loser, 409, "SCHEDULE_CONFLICT")
    assert await lesson_count(database) == 9


async def test_read_committed_isolation(database: Database) -> None:
    async with database.factory() as session:
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"


async def test_workload_query_covers_both_local_days(
    running: RunningApp, database: Database
) -> None:
    assert running.context.people_client is not None
    await running.context.people_client.aclose()
    profile = {
        "id": str(I1),
        "full_name": "I1",
        "max_hours_per_day": 2,
        "timezone": "Asia/Yekaterinburg",
    }
    running.context.people_client = PeopleClient(
        Settings(), transport=CountingTransport(overrides={f"/instructors/{I1}": profile})
    )
    async with database.factory() as session, session.begin():
        for uid, start, end in [
            ("3d000000-0000-4000-8000-000000000091", 16, 18),
            ("3d000000-0000-4000-8000-000000000092", 21, 23),
        ]:
            session.add(
                Lesson(
                    id=UUID(uid),
                    group_id=G1,
                    instructor_id=I1,
                    classroom_id=None,
                    format=LessonFormat.ONLINE,
                    status=LessonStatus.PLANNED,
                    starts_at=datetime(2026, 12, 9, start, tzinfo=UTC),
                    ends_at=datetime(2026, 12, 9, end, tzinfo=UTC),
                    rescheduled_from_id=None,
                    cancel_reason=None,
                    created_at=running.context.clock.now(),
                    updated_at=running.context.clock.now(),
                )
            )
    body = BODY | {
        "format": "ONLINE",
        "classroom_id": None,
        "starts_at": "2026-12-09T18:30:00Z",
        "ends_at": "2026-12-09T20:30:00Z",
    }
    response = await running.client.post("/lessons/validate", json=body)
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "ValidationResult")
    assert response.json()["ok"] is True
    assert [
        issue["details"]
        for issue in response.json()["warnings"]
        if issue["code"] == "WORKLOAD_SOFT_LIMIT"
    ] == [
        {"date": "2026-12-09", "max_hours": 2.0, "planned_hours": 2.5},
        {"date": "2026-12-10", "max_hours": 2.0, "planned_hours": 3.5},
    ]
