"""FR-03/05: SQL-выборки, полный E2E-02, PATCH и конкуренция на PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.people import PeopleClient
from app.domain.lesson import LessonStatus
from app.domain.listing import LessonFilters
from app.models import Lesson
from app.repositories import listing
from app.settings import Settings
from tests.e2e.public import test_e2e_02_instructor_conflict as public_conflict
from tests.e2e.report import Recorder, ScenarioRecord
from tests.unit.scheduling_data import C1, G1, I1, I2, L1
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_listing_and_patch import row
from tests.unit.test_people_client import CountingTransport
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database
from tests.unit.test_wp03_postgres import RunningApp
from tests.unit.test_wp03_postgres import running as running

L3 = UUID(int=L1.int + 2)
EXTRA = UUID("4e000000-0000-4000-8000-000000000000")


def lid(index: int) -> str:
    return str(UUID(int=L1.int + index - 1))


async def get_json(running: RunningApp, lesson_id: UUID = L1) -> dict[str, Any]:
    response = await running.client.get(f"/lessons/{lesson_id}")
    assert response.status_code == 200
    return dict(response.json())


async def test_full_public_e2e02_source(running: RunningApp) -> None:
    await public_conflict.test_instructor_conflict_is_rejected_and_nothing_is_created(
        running.client,
        {"G3": "9a000000-0000-4000-8000-000000000003", "I1": str(I1), "L1": str(L1)},
        CONTRACT.fork(),
        Recorder(ScenarioRecord("E2E-02", "Конфликт и список", True)),
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, [8, 4, 1, 2, 3, 5, 6, 7]),
        ({"group_id": str(G1)}, [4, 1]),
        ({"instructor_id": str(I1)}, [1, 5, 6, 7]),
        ({"classroom_id": str(C1)}, [1, 2, 5]),
        ({"status": "PLANNED"}, [1, 3, 7]),
        ({"from": "2026-12-01T07:00:00Z", "to": "2026-12-03T05:00:00Z"}, [2, 3, 5]),
        ({"from": "2026-12-01T12:00:00+05:00", "to": "2026-12-03T10:00:00+05:00"}, [2, 3, 5]),
        ({"from": "2026-12-01T05:30:00Z", "to": "2026-12-01T07:00:00Z"}, []),
        ({"group_id": str(G1), "instructor_id": str(I1), "status": "PLANNED"}, [1]),
        ({"group_id": "9a000000-0000-4000-8000-000000000099"}, []),
    ],
)
async def test_list_seed_filters(
    running: RunningApp, params: dict[str, str], expected: list[int]
) -> None:
    response = await running.client.get("/lessons", params=params)
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "LessonPage")
    assert [item["id"] for item in response.json()["items"]] == [lid(i) for i in expected]
    assert response.json()["total"] == len(expected)


@pytest.mark.parametrize(("limit", "offset", "expected"), [(2, 1, [4, 1]), (200, 100, [])])
async def test_page_total_is_unpaginated(
    running: RunningApp, limit: int, offset: int, expected: list[int]
) -> None:
    response = await running.client.get("/lessons", params={"limit": limit, "offset": offset})
    assert response.status_code == 200
    assert response.json()["total"] == 8
    assert [item["id"] for item in response.json()["items"]] == [lid(i) for i in expected]


async def test_default_limit_50_and_uuid_tie_order(running: RunningApp, database: Database) -> None:
    async with database.factory() as session, session.begin():
        for index in range(60):
            item = row()
            item.id = UUID(int=EXTRA.int + index + 1)
            item.starts_at = datetime(2027, 1, 1, tzinfo=UTC) + timedelta(hours=index * 2)
            item.ends_at = item.starts_at + timedelta(hours=1)
            session.add(item)
    response = await running.client.get("/lessons")
    assert response.status_code == 200
    assert response.json()["total"] == 68
    assert len(response.json()["items"]) == 50
    # Два ряда с одним starts_at созданы в обратном порядке UUID.
    async with database.factory() as session, session.begin():
        for index in (101, 100):
            item = row()
            item.id = UUID(int=EXTRA.int + index)
            item.starts_at = datetime(2027, 2, 1, 7, tzinfo=UTC)
            item.ends_at = item.starts_at + timedelta(hours=1)
            if index == 101:
                item.group_id = UUID("9a000000-0000-4000-8000-000000000003")
                item.instructor_id = UUID("1b000000-0000-4000-8000-000000000003")
                item.classroom_id = UUID("2c000000-0000-4000-8000-000000000003")
            session.add(item)
    response = await running.client.get(
        "/lessons",
        params={
            "from": "2027-02-01T00:00:00Z",
            "to": "2027-02-02T00:00:00Z",
            "limit": 1,
            "offset": 1,
        },
    )
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["id"] == str(UUID(int=EXTRA.int + 101))


async def test_list_uses_one_snapshot_for_count_and_items(
    running: RunningApp, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = listing.get_page

    async def add_after_snapshot(
        session: AsyncSession, filters: LessonFilters
    ) -> tuple[list[Lesson], int]:
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        await session.execute(select(Lesson.id).limit(1))
        async with database.factory() as writer, writer.begin():
            item = row()
            item.id = UUID(int=EXTRA.int + 999)
            writer.add(item)
        return await original(session, filters)

    monkeypatch.setattr(listing, "get_page", add_after_snapshot)
    response = await running.client.get("/lessons")
    assert response.json()["total"] == 8
    assert len(response.json()["items"]) == 8
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 9
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"


async def test_partial_patch_preserves_fields_and_updates_clock(running: RunningApp) -> None:
    before = await get_json(running)
    running.context.clock.freeze(datetime(2026, 11, 30, 4, tzinfo=UTC))
    response = await running.client.patch(
        f"/lessons/{L1}", json={"ends_at": "2026-12-01T06:00:00Z"}
    )
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "LessonWithWarnings")
    after = response.json()["lesson"]
    assert after == before | {
        "ends_at": "2026-12-01T06:00:00Z",
        "updated_at": "2026-11-30T04:00:00Z",
    }
    assert await get_json(running) == after


async def test_full_patch_and_explicit_online_null(running: RunningApp) -> None:
    before = await get_json(running)
    body = {
        "starts_at": "2026-12-02T12:00:00+03:00",
        "ends_at": "2026-12-02T13:00:00+03:00",
        "instructor_id": "1b000000-0000-4000-8000-000000000003",
        "classroom_id": "2c000000-0000-4000-8000-000000000003",
        "format": "IN_PERSON",
    }
    response = await running.client.patch(f"/lessons/{L1}", json=body)
    assert response.status_code == 200, response.text
    lesson = response.json()["lesson"]
    assert lesson["starts_at"] == "2026-12-02T09:00:00Z"
    assert lesson["created_at"] == before["created_at"]
    assert lesson["group_id"] == before["group_id"]
    assert lesson["instructor_id"] == body["instructor_id"]
    assert running.context.people_client is not None
    await running.context.people_client.aclose()
    transport = CountingTransport()
    running.context.people_client = PeopleClient(Settings(), transport=transport)
    response = await running.client.patch(
        f"/lessons/{L1}", json={"format": "ONLINE", "classroom_id": None}
    )
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "LessonWithWarnings")
    assert response.json()["lesson"]["classroom_id"] is None
    assert len(transport.paths) == 2


@pytest.mark.parametrize(
    ("lesson_id", "body", "code"),
    [
        (L1, {"classroom_id": "2c000000-0000-4000-8000-000000000002"}, "CLASSROOM_TOO_SMALL"),
        (
            L3,
            {
                "instructor_id": str(I1),
                "starts_at": "2026-12-01T05:30:00Z",
                "ends_at": "2026-12-01T06:30:00Z",
            },
            "SCHEDULE_CONFLICT",
        ),
    ],
)
async def test_failed_patch_is_atomic(
    running: RunningApp, lesson_id: UUID, body: dict[str, str], code: str
) -> None:
    before = await get_json(running, lesson_id)
    response = await running.client.patch(f"/lessons/{lesson_id}", json=body)
    CONTRACT.assert_error(response, 409, code)
    assert await get_json(running, lesson_id) == before


class GateTransport(CountingTransport):
    def __init__(self, target: int) -> None:
        super().__init__()
        self.target = target
        self.started = 0
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started += 1
        if self.started >= self.target:
            self.arrived.set()
        await self.release.wait()
        return await super().handle_async_request(request)


async def gated_people(running: RunningApp, target: int) -> GateTransport:
    assert running.context.people_client is not None
    await running.context.people_client.aclose()
    transport = GateTransport(target)
    running.context.people_client = PeopleClient(
        Settings(people_cache_ttl_seconds=0), transport=transport
    )
    return transport


@pytest.mark.parametrize(
    "change", [{"starts_at": "2026-12-01T05:15:00Z"}, {"instructor_id": str(I2)}]
)
async def test_concurrent_partial_patch_remerges_fresh_state(
    running: RunningApp, database: Database, change: dict[str, str]
) -> None:
    # Одинаковый updated_at до и после: алгоритм обязан сравнивать поля, а не только Clock/version.
    async with database.factory() as session, session.begin():
        await session.execute(
            update(Lesson).where(Lesson.id == L1).values(updated_at=running.context.clock.now())
        )
    transport = await gated_people(running, 6)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        a = tasks.create_task(running.client.patch(f"/lessons/{L1}", json=change))
        b = tasks.create_task(
            running.client.patch(f"/lessons/{L1}", json={"ends_at": "2026-12-01T06:00:00Z"})
        )
        await transport.arrived.wait()
        transport.release.set()
    assert a.result().status_code == 200, a.result().text
    assert b.result().status_code == 200, b.result().text
    after = await get_json(running)
    assert after["ends_at"] == "2026-12-01T06:00:00Z"
    for name, value in change.items():
        assert after[name] == value
    assert transport.started >= 9  # Произошёл повтор после обнаружения изменённого снимка.


async def test_concurrent_patch_to_same_slot_has_one_winner(
    running: RunningApp, database: Database
) -> None:
    body = {
        "starts_at": "2026-12-10T07:00:00Z",
        "ends_at": "2026-12-10T08:00:00Z",
        "format": "ONLINE",
        "classroom_id": None,
        "instructor_id": str(I1),
    }
    transport = await gated_people(running, 4)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        a = tasks.create_task(running.client.patch(f"/lessons/{L1}", json=body))
        b = tasks.create_task(running.client.patch(f"/lessons/{L3}", json=body))
        await transport.arrived.wait()
        transport.release.set()
    assert sorted([a.result().status_code, b.result().status_code]) == [200, 409]
    async with database.factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Lesson)
            .where(Lesson.starts_at == datetime(2026, 12, 10, 7, tzinfo=UTC))
        )
        assert count == 1


@pytest.mark.parametrize("remove", [False, True])
async def test_locked_row_is_rechecked_after_http(
    running: RunningApp, database: Database, remove: bool
) -> None:
    transport = await gated_people(running, 3)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        task = tasks.create_task(
            running.client.patch(f"/lessons/{L1}", json={"ends_at": "2026-12-01T06:00:00Z"})
        )
        await transport.arrived.wait()
        async with database.factory() as session, session.begin():
            lesson = await session.get(Lesson, L1)
            assert lesson is not None
            if remove:
                await session.delete(lesson)
            else:
                lesson.status = LessonStatus.CONFIRMED
        transport.release.set()
    response = task.result()
    CONTRACT.assert_error(
        response, 404 if remove else 409, "NOT_FOUND" if remove else "LESSON_NOT_EDITABLE"
    )
    if not remove:
        assert (await get_json(running))["ends_at"] == "2026-12-01T06:30:00Z"
