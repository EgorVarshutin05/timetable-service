"""FR-13: PostgreSQL commit наблюдаем до publish; E2E-17 с перехватом сообщения."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.domain.events import LessonEvent
from app.domain.lesson import LessonStatus
from app.models import Lesson
from app.seed import load_seed, read_all
from tests.e2e.public import test_e2e_17_confirm_event as public_confirm
from tests.e2e.report import Recorder, ScenarioRecord
from tests.unit.event_helpers import CapturingPublisher
from tests.unit.scheduling_data import I2, L1
from tests.unit.test_lesson_commands_api import BODY, CONTRACT
from tests.unit.test_lesson_lifecycle import MOVE
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database
from tests.unit.test_wp03_postgres import RunningApp
from tests.unit.test_wp03_postgres import running as running
from tests.unit.test_wp04_postgres import gated_people, get_json

L4 = UUID(int=L1.int + 3)


class CaptureConsumer:
    """E2E BrokerHelper-адаптер: читает перехватчик вместо сетевого брокера."""

    def __init__(self, publisher: CapturingPublisher) -> None:
        self.publisher = publisher

    async def publish(self, source: str, event: dict[str, Any], *, key: str | None = None) -> None:
        raise AssertionError("E2E-17 не должен отправлять чужие события")

    async def consume(
        self, timeout_seconds: float, *, max_messages: int | None = None
    ) -> list[dict[str, Any]]:
        return await self.publisher.consume(timeout_seconds, max_messages=max_messages)

    async def purge(self) -> None:
        await self.publisher.purge()

    async def aclose(self) -> None:
        pass


def capture(running: RunningApp) -> CapturingPublisher:
    publisher = running.context.event_publisher
    assert isinstance(publisher, CapturingPublisher)
    return publisher


async def test_full_public_e2e17_source_with_captured_message(running: RunningApp) -> None:
    await public_confirm.test_confirm_publishes_exactly_one_event(
        running.client,
        CaptureConsumer(capture(running)),
        {
            "L1": str(L1),
            "G1": str(BODY["group_id"]),
            "I1": str(BODY["instructor_id"]),
            "C1": str(BODY["classroom_id"]),
        },
        CONTRACT.fork(),
        Recorder(ScenarioRecord("E2E-17", "Подтверждение и событие", True)),
    )


@pytest.mark.parametrize("operation", ["confirm", "cancel", "reschedule"])
async def test_independent_session_sees_commit_before_publish(
    running: RunningApp, database: Database, operation: str
) -> None:
    publisher = CapturingPublisher()
    now = running.context.clock.now()

    class CheckingPublisher:
        async def publish(self, event: LessonEvent) -> None:
            async with database.factory() as session:
                original = await session.get(Lesson, L1)
                assert original is not None
                target = {
                    "confirm": LessonStatus.CONFIRMED,
                    "cancel": LessonStatus.CANCELLED,
                    "reschedule": LessonStatus.RESCHEDULED,
                }[operation]
                assert original.status == target
                if operation == "reschedule":
                    child = await session.get(Lesson, UUID(event.payload["rescheduled_to_id"]))
                    assert child is not None
                    assert child.rescheduled_from_id == L1
            # Clock уже может измениться между commit и I/O. occurred_at — момент операции.
            running.context.clock.freeze(datetime(2030, 1, 1, tzinfo=UTC))
            assert event.occurred_at == now
            await publisher.publish(event)

    running.context.event_publisher = CheckingPublisher()
    body: dict[str, Any] | None = None
    if operation == "reschedule":
        body = MOVE | {"instructor_id": str(I2), "format": "ONLINE", "classroom_id": None}
    elif operation == "cancel":
        body = {"reason": "Причина"}
    response = await running.client.post(f"/lessons/{L1}/{operation}", json=body)
    assert response.status_code == (201 if operation == "reschedule" else 200), response.text
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event["occurred_at"] == "2026-11-30T03:00:00Z"
    if operation == "reschedule":
        assert event["payload"]["instructor_id"] == BODY["instructor_id"]
        assert event["payload"]["previous"]["starts_at"] == "2026-12-01T05:00:00Z"
        assert event["payload"]["new"]["instructor_id"] == str(I2)
        assert event["payload"]["new"]["classroom_id"] is None
        assert event["payload"]["rescheduled_to_id"] == response.json()["lesson"]["id"]
    elif operation == "cancel":
        assert event["payload"]["previous_status"] == "PLANNED"
        assert event["payload"]["reason"] == "Причина"


@pytest.mark.parametrize("operation", ["confirm", "cancel", "reschedule"])
@pytest.mark.parametrize("failure", ["flush", "commit"])
async def test_failed_transaction_never_publishes(
    running: RunningApp,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
) -> None:
    before = await get_json(running)

    async def failed(self: AsyncSession, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"{failure} failed")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, failure, failed)
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            await running.client.post(
                f"/lessons/{L1}/{operation}", json=MOVE if operation == "reschedule" else None
            )
    assert capture(running).events == []
    assert await get_json(running) == before
    async with database.factory() as session:
        assert (
            await session.scalar(select(Lesson.id).where(Lesson.rescheduled_from_id == L1)) is None
        )


async def test_other_operations_do_not_publish(running: RunningApp, database: Database) -> None:
    publisher = capture(running)
    created = await running.client.post("/lessons", json=BODY)
    assert created.status_code == 201
    assert publisher.events == []
    assert (await running.client.post("/lessons/validate", json=BODY)).status_code == 200
    assert publisher.events == []
    lesson_id = created.json()["lesson"]["id"]
    patched = await running.client.patch(
        f"/lessons/{lesson_id}", json={"ends_at": "2026-12-02T08:00:00Z"}
    )
    assert patched.status_code == 200
    assert publisher.events == []
    running.context.clock.freeze(datetime(2026, 12, 4, tzinfo=UTC))
    swept = await running.client.post("/__test/jobs/lesson-status-sweeper/run")
    assert swept.json() == {"processed": 2}
    assert publisher.events == []
    completed = await running.client.post(f"/lessons/{L4}/complete")
    assert completed.status_code == 200
    assert publisher.events == []
    async with database.factory() as session, session.begin():
        await load_seed(session, read_all())
    assert publisher.events == []
    assert (await running.client.post("/__test/reset")).status_code == 204
    assert publisher.events == []


@pytest.mark.parametrize("operation", ["confirm", "cancel", "reschedule"])
async def test_forbidden_operations_never_publish(running: RunningApp, operation: str) -> None:
    # COMPLETED L8 запрещает все три команды.
    lesson_id = UUID(int=L1.int + 7)
    response = await running.client.post(
        f"/lessons/{lesson_id}/{operation}", json=MOVE if operation == "reschedule" else None
    )
    CONTRACT.assert_error(response, 409, "TRANSITION_FORBIDDEN")
    assert capture(running).events == []


async def test_racing_reschedules_publish_only_winner(running: RunningApp) -> None:
    gate = await gated_people(running, 6)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        a = tasks.create_task(running.client.post(f"/lessons/{L1}/reschedule", json=MOVE))
        b = tasks.create_task(running.client.post(f"/lessons/{L1}/reschedule", json=MOVE))
        await gate.arrived.wait()
        gate.release.set()
    assert sorted([a.result().status_code, b.result().status_code]) == [201, 409]
    published = capture(running).events
    assert len(published) == 1
    assert published[0]["event_type"] == "LessonRescheduled"


async def test_publisher_failure_is_postcommit_without_transaction_replay(
    running: RunningApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = capture(running)
    failed = AsyncMock(side_effect=OSError("broker down"))
    monkeypatch.setattr(publisher, "publish", failed)
    app = main.create_app()
    app.state.context = running.context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post(f"/lessons/{L1}/confirm")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert (await get_json(running))["status"] == "CONFIRMED"
    assert publisher.events == []
    repeated = await running.client.post(f"/lessons/{L1}/confirm")
    CONTRACT.assert_error(repeated, 409, "TRANSITION_FORBIDDEN")
    failed.assert_awaited_once()


async def test_confirmed_cancellation_event_captures_locked_previous_status(
    running: RunningApp,
) -> None:
    assert (await running.client.post(f"/lessons/{L1}/confirm")).status_code == 200
    publisher = capture(running)
    await publisher.purge()
    cancelled = await running.client.post(f"/lessons/{L1}/cancel", json={"reason": None})
    assert cancelled.status_code == 200
    assert len(publisher.events) == 1
    assert publisher.events[0]["payload"]["previous_status"] == "CONFIRMED"
    assert publisher.events[0]["payload"]["reason"] is None
