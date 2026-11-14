"""FR-06..10: транзакции, управляемые часы и конкурентные команды на PostgreSQL."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.lesson import LessonFormat, LessonStatus
from app.errors import ApiError
from app.models import Lesson
from app.repositories import scheduling
from tests.unit.scheduling_data import I2, L1
from tests.unit.test_lesson_commands_api import BODY, CONTRACT
from tests.unit.test_lesson_lifecycle import MOVE, TARGETS
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database
from tests.unit.test_wp03_postgres import RunningApp
from tests.unit.test_wp03_postgres import running as running
from tests.unit.test_wp04_postgres import gated_people, get_json

S = LessonStatus
L2 = UUID(int=L1.int + 1)
L4 = UUID(int=L1.int + 3)
JOB = "/__test/jobs/lesson-status-sweeper/run"


async def test_confirm_once_and_repeat_forbidden(running: RunningApp) -> None:
    before = await get_json(running)
    confirmed = await running.client.post(f"/lessons/{L1}/confirm")
    assert confirmed.status_code == 200
    CONTRACT.assert_schema(confirmed.json(), "Lesson")
    assert confirmed.json() == before | {
        "status": "CONFIRMED",
        "updated_at": "2026-11-30T03:00:00Z",
    }
    repeat = await running.client.post(f"/lessons/{L1}/confirm")
    assert CONTRACT.assert_error(repeat, 409, "TRANSITION_FORBIDDEN") == {
        "current_status": "CONFIRMED",
        "target_status": "CONFIRMED",
    }
    assert await get_json(running) == confirmed.json()


async def test_full_lifecycle_with_clock_end_boundary(running: RunningApp) -> None:
    created = await running.client.post("/lessons", json=BODY)
    assert created.status_code == 201
    lesson_id = created.json()["lesson"]["id"]
    path = f"/lessons/{lesson_id}"
    before = await get_json(running, UUID(lesson_id))
    confirmed = await running.client.post(path + "/confirm")
    assert confirmed.status_code == 200
    CONTRACT.assert_schema(confirmed.json(), "Lesson")
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["created_at"] == before["created_at"]
    assert (await running.client.post(JOB)).json() == {"processed": 0}
    await running.client.put("/__test/time", json={"now": before["starts_at"]})
    assert (await running.client.post(JOB)).json() == {"processed": 3}  # L2, L4, новое.
    assert (await get_json(running, UUID(lesson_id)))["status"] == "IN_PROGRESS"
    early = await running.client.post(path + "/complete")
    assert CONTRACT.assert_error(early, 409, "LESSON_NOT_FINISHED") == {
        "ends_at": before["ends_at"],
        "now": before["starts_at"],
    }
    await running.client.put("/__test/time", json={"now": before["ends_at"]})
    done = await running.client.post(path + "/complete")
    assert done.status_code == 200
    CONTRACT.assert_schema(done.json(), "Lesson")
    assert done.json()["status"] == "COMPLETED"
    assert done.json()["updated_at"] == before["ends_at"]
    assert (await running.client.post(JOB)).json() == {"processed": 0}
    repeat = await running.client.post(path + "/complete")
    assert CONTRACT.assert_error(repeat, 409, "TRANSITION_FORBIDDEN") == {
        "current_status": "COMPLETED",
        "target_status": "COMPLETED",
    }


@pytest.mark.parametrize("status", [S.PLANNED, S.CONFIRMED])
@pytest.mark.parametrize(
    "body", [None, {}, {"reason": None}, {"reason": "Причина"}, {"reason": "x" * 500}]
)
async def test_cancel_optional_reason_persisted(
    running: RunningApp, database: Database, status: S, body: dict[str, Any] | None
) -> None:
    async with database.factory() as session, session.begin():
        await session.execute(update(Lesson).where(Lesson.id == L1).values(status=status))
    before = await get_json(running)
    response = await running.client.post(f"/lessons/{L1}/cancel", json=body)
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "Lesson")
    assert response.json() == before | {
        "status": "CANCELLED",
        "cancel_reason": body.get("reason") if body else None,
        "updated_at": "2026-11-30T03:00:00Z",
    }
    assert await get_json(running) == response.json()


@pytest.mark.parametrize("status", [S.PLANNED, S.CONFIRMED])
@pytest.mark.parametrize(
    "changes", [{}, {"format": "ONLINE", "classroom_id": None, "instructor_id": str(I2)}]
)
async def test_atomic_reschedule_inherits_or_replaces_fields(
    running: RunningApp, database: Database, status: S, changes: dict[str, Any]
) -> None:
    async with database.factory() as session, session.begin():
        await session.execute(update(Lesson).where(Lesson.id == L1).values(status=status))
    before = await get_json(running)
    moved = await running.client.post(f"/lessons/{L1}/reschedule", json=MOVE | changes)
    assert moved.status_code == 201, moved.text
    CONTRACT.assert_schema(moved.json(), "LessonWithWarnings")
    new = moved.json()["lesson"]
    UUID(new["id"])
    assert new["id"] != str(L1)
    assert new["group_id"] == before["group_id"]
    assert new["rescheduled_from_id"] == str(L1)
    assert new["status"] == "PLANNED"
    assert new["cancel_reason"] is None
    assert new["created_at"] == new["updated_at"] == "2026-11-30T03:00:00Z"
    for name in ("instructor_id", "format", "classroom_id"):
        assert new[name] == changes.get(name, before[name])
    assert await get_json(running) == before | {
        "status": "RESCHEDULED",
        "updated_at": "2026-11-30T03:00:00Z",
    }
    assert await get_json(running, UUID(new["id"])) == new
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 9


async def test_reschedule_excludes_self_from_conflicts_and_workload(
    running: RunningApp, database: Database
) -> None:
    # 4h + те же 4h ошибочно дали бы 8 > max=6; корректная нагрузка ровно 4.
    async with database.factory() as session, session.begin():
        await session.execute(
            update(Lesson)
            .where(Lesson.id == L1)
            .values(
                ends_at=datetime(2026, 12, 1, 9, tzinfo=UTC),
                format=LessonFormat.ONLINE,
                classroom_id=None,
            )
        )
    response = await running.client.post(
        f"/lessons/{L1}/reschedule",
        json={
            "starts_at": "2026-12-01T05:00:00Z",
            "ends_at": "2026-12-01T09:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["warnings"] == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (MOVE | {"classroom_id": "2c000000-0000-4000-8000-000000000002"}, "CLASSROOM_TOO_SMALL"),
        (
            {"starts_at": "2026-12-01T07:30:00Z", "ends_at": "2026-12-01T08:30:00Z"},
            "SCHEDULE_CONFLICT",
        ),
    ],
)
async def test_reschedule_rule_failure_rolls_back_both_sides(
    running: RunningApp, database: Database, body: dict[str, str], code: str
) -> None:
    before = await get_json(running)
    response = await running.client.post(f"/lessons/{L1}/reschedule", json=body)
    CONTRACT.assert_error(response, 409, code)
    assert await get_json(running) == before
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 8


async def test_reschedule_flush_failure_rolls_back_original_status(
    running: RunningApp, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await get_json(running)

    async def failed_flush(self: AsyncSession, objects: Any = None) -> None:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(AsyncSession, "flush", failed_flush)
    with pytest.raises(RuntimeError, match="insert failed"):
        await running.client.post(f"/lessons/{L1}/reschedule", json=MOVE)
    monkeypatch.undo()
    assert await get_json(running) == before
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 8


async def test_two_reschedules_have_exactly_one_successor(
    running: RunningApp, database: Database
) -> None:
    transport = await gated_people(running, 6)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        a = tasks.create_task(running.client.post(f"/lessons/{L1}/reschedule", json=MOVE))
        b = tasks.create_task(running.client.post(f"/lessons/{L1}/reschedule", json=MOVE))
        await transport.arrived.wait()
        transport.release.set()
    responses = sorted([a.result(), b.result()], key=lambda r: r.status_code)
    assert [r.status_code for r in responses] == [201, 409]
    assert CONTRACT.assert_error(responses[1], 409, "TRANSITION_FORBIDDEN") == {
        "current_status": "RESCHEDULED",
        "target_status": "RESCHEDULED",
    }
    async with database.factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Lesson).where(Lesson.rescheduled_from_id == L1)
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 9


async def test_confirm_cancel_race_serializes_on_row(
    running: RunningApp, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = scheduling.get_lesson
    arrived = asyncio.Event()
    calls = 0

    async def track(session: AsyncSession, lesson_id: UUID, *, lock: bool = False) -> Lesson | None:
        nonlocal calls
        if lock:
            calls += 1
            if calls == 2:
                arrived.set()
        return await original(session, lesson_id, lock=lock)

    monkeypatch.setattr(scheduling, "get_lesson", track)
    async with (
        asyncio.timeout(5),
        asyncio.TaskGroup() as tasks,
        database.factory() as blocker,
        blocker.begin(),
    ):
        await original(blocker, L1, lock=True)
        confirm = tasks.create_task(running.client.post(f"/lessons/{L1}/confirm"))
        cancel = tasks.create_task(running.client.post(f"/lessons/{L1}/cancel"))
        await arrived.wait()
    assert cancel.result().status_code == 200
    assert confirm.result().status_code in (200, 409)
    if confirm.result().status_code == 409:
        CONTRACT.assert_error(confirm.result(), 409, "TRANSITION_FORBIDDEN")
    assert (await get_json(running))["status"] == "CANCELLED"


@pytest.mark.parametrize(
    ("now", "processed"),
    [
        ("2026-11-30T03:59:59Z", 0),
        ("2026-11-30T04:00:00Z", 1),
        ("2026-12-04T00:00:00Z", 2),
    ],
)
async def test_sweeper_is_idempotent_and_includes_already_ended(
    running: RunningApp, now: str, processed: int
) -> None:
    before = (await running.client.get("/lessons")).json()["items"]
    await running.client.put("/__test/time", json={"now": now})
    assert (await running.client.post(JOB)).json() == {"processed": processed}
    assert (await running.client.post(JOB)).json() == {"processed": 0}
    after = {item["id"]: item for item in (await running.client.get("/lessons")).json()["items"]}
    for item in before:
        if item["status"] == "CONFIRMED" and item["starts_at"] <= now:
            assert after[item["id"]] == item | {"status": "IN_PROGRESS", "updated_at": now}
        else:
            assert after[item["id"]] == item


async def test_concurrent_sweepers_count_each_row_once(running: RunningApp) -> None:
    await running.client.put("/__test/time", json={"now": "2026-12-04T00:00:00Z"})
    async with asyncio.timeout(5):
        responses = await asyncio.gather(running.client.post(JOB), running.client.post(JOB))
    assert sorted(r.json()["processed"] for r in responses) == [0, 2]


@pytest.mark.parametrize("operation", ["cancel", "reschedule"])
async def test_sweeper_serializes_with_lifecycle_operation(
    running: RunningApp, operation: str
) -> None:
    await running.client.put("/__test/time", json={"now": "2026-11-30T06:00:00Z"})
    async with asyncio.timeout(5):
        result, swept = await asyncio.gather(
            running.client.post(
                f"/lessons/{L4}/{operation}",
                json=MOVE if operation == "reschedule" else None,
            ),
            running.client.post(JOB),
        )
    assert swept.status_code == 200
    if result.status_code == 409:
        assert CONTRACT.assert_error(result, 409, "TRANSITION_FORBIDDEN") == {
            "current_status": "IN_PROGRESS",
            "target_status": TARGETS[operation].value,
        }
        assert swept.json() == {"processed": 1}
    else:
        assert result.status_code == (201 if operation == "reschedule" else 200), result.text
        assert swept.json() == {"processed": 0}
        assert (await get_json(running, L4))["status"] == TARGETS[operation].value


async def test_reschedule_rechecks_cancelled_status_after_http(running: RunningApp) -> None:
    transport = await gated_people(running, 3)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(running.client.post(f"/lessons/{L1}/reschedule", json=MOVE))
        await transport.arrived.wait()
        assert (await running.client.post(f"/lessons/{L1}/cancel")).status_code == 200
        transport.release.set()
    assert CONTRACT.assert_error(pending.result(), 409, "TRANSITION_FORBIDDEN") == {
        "current_status": "CANCELLED",
        "target_status": "RESCHEDULED",
    }


async def test_reschedule_retries_when_patch_changes_inherited_resources(
    running: RunningApp,
) -> None:
    transport = await gated_people(running, 3)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(running.client.post(f"/lessons/{L1}/reschedule", json=MOVE))
        await transport.arrived.wait()
        # Временно открываем HTTP, PATCH должен завершиться раньше возобновления переноса:
        # меняем данные через ту же команду, удержав первый HTTP отдельной точкой.
        assert running.context.people_client is not None
        original = running.context.people_client
        from app.clients.people import PeopleClient
        from app.settings import Settings
        from tests.unit.test_people_client import CountingTransport

        async with PeopleClient(Settings(), transport=CountingTransport()) as other:
            running.context.people_client = other
            changed = await running.client.patch(f"/lessons/{L1}", json={"instructor_id": str(I2)})
            assert changed.status_code == 200, changed.text
        running.context.people_client = original
        transport.release.set()
    assert pending.result().status_code == 201, pending.result().text
    assert pending.result().json()["lesson"]["instructor_id"] == str(I2)
    assert transport.started >= 6


@pytest.mark.parametrize(
    ("status", "code"),
    [(422, "INSTRUCTOR_NOT_FOUND"), (502, "UPSTREAM_UNAVAILABLE"), (504, "UPSTREAM_TIMEOUT")],
)
async def test_reschedule_upstream_failure_preserves_state(
    running: RunningApp, database: Database, monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    assert running.context.people_client is not None
    monkeypatch.setattr(
        running.context.people_client,
        "get_lesson_people",
        AsyncMock(side_effect=ApiError(status, code, "upstream failed")),
    )
    before = await get_json(running)
    result = await running.client.post(f"/lessons/{L1}/reschedule", json=MOVE)
    CONTRACT.assert_error(result, status, code)
    assert await get_json(running) == before
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 8
