"""FR-16/17: seed, name/id ordering, snapshot and reset with PostgreSQL."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.clients.people import PeopleClient
from app.domain.lesson import LessonFormat
from app.models import Group, Lesson
from app.settings import Settings
from tests.unit.event_helpers import CapturingPublisher
from tests.unit.scheduling_data import C1, G1, G2, I1
from tests.unit.test_catalog_api import CONTRACT, DATA
from tests.unit.test_lesson_commands_api import BODY
from tests.unit.test_people_cache import Timer
from tests.unit.test_people_client import INSTRUCTORS, PROFILE, CountingTransport
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database
from tests.unit.test_wp03_postgres import RunningApp
from tests.unit.test_wp03_postgres import running as running

G3 = UUID(int=G1.int + 2)


async def page(running: RunningApp, **params: int) -> dict[str, Any]:
    response = await running.client.get("/groups", params=params)
    assert response.status_code == 200, response.text
    CONTRACT.assert_schema(response.json(), "GroupPage")
    return dict(response.json())


async def test_seed_name_order_differs_from_id_order(running: RunningApp) -> None:
    result = await page(running)
    assert result == {
        "items": [DATA["groups"][1], DATA["groups"][2], DATA["groups"][0]],
        "total": 3,
    }
    for item in result["items"]:
        found = await running.client.get(f"/groups/{item['id']}")
        assert found.json() == item
    publisher = running.context.event_publisher
    assert isinstance(publisher, CapturingPublisher)
    assert publisher.events == []


@pytest.mark.parametrize(
    ("limit", "offset", "ids"),
    [(1, 0, [G2]), (1, 1, [G3]), (2, 1, [G3, G1]), (200, 2, [G1]), (50, 3, []), (50, 999, [])],
)
async def test_page_slice_keeps_total(
    running: RunningApp, limit: int, offset: int, ids: list[UUID]
) -> None:
    result = await page(running, limit=limit, offset=offset)
    assert result["total"] == 3
    assert [item["id"] for item in result["items"]] == list(map(str, ids))


async def test_name_ties_use_ascending_uuid(running: RunningApp, database: Database) -> None:
    async with database.factory() as session, session.begin():
        await session.execute(update(Group).values(name="same"))
    result = await page(running)
    assert [item["id"] for item in result["items"]] == [str(G1), str(G2), str(G3)]
    assert [item["id"] for item in (await page(running, limit=1, offset=1))["items"]] == [str(G2)]


async def test_empty_database(running: RunningApp, database: Database) -> None:
    async with database.factory() as session, session.begin():
        await session.execute(delete(Lesson))
        await session.execute(delete(Group))
    assert await page(running) == {"items": [], "total": 0}


async def test_count_and_items_share_snapshot_during_insert(
    running: RunningApp,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AsyncSession.scalar
    inserted = False

    async def count_then_insert(
        self: AsyncSession, statement: Executable, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal inserted
        result = await original(self, statement, *args, **kwargs)
        if not inserted:
            inserted = True
            async with database.factory() as writer, writer.begin():
                writer.add(Group(id=UUID(int=999), name="AAA", size=1))
        return result

    monkeypatch.setattr(AsyncSession, "scalar", count_then_insert)
    result = await page(running)
    assert inserted
    assert result["total"] == len(result["items"]) == 3
    # Следующий HTTP-запрос получает новый снимок и видит commit writer.
    assert (await page(running))["total"] == 4


async def test_reset_clears_all_people_entries_and_preserves_clock(running: RunningApp) -> None:
    context = running.context
    assert context.people_client is not None
    await context.people_client.aclose()
    transport = CountingTransport()
    context.people_client = PeopleClient(Settings(), transport=transport)
    client = context.people_client
    await client.get_lesson_people(
        instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
    )
    await client.get_lesson_people(
        instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
    )
    assert len(transport.paths) == 3
    before = context.clock.now()
    transport.overrides[PROFILE] = INSTRUCTORS[str(I1)] | {"max_hours_per_day": 2}
    response = await running.client.post("/__test/reset")
    assert response.status_code == 204
    assert context.clock.now() == before
    result = await client.get_lesson_people(
        instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
    )
    assert result.instructor.max_hours_per_day == 2
    assert len(transport.paths) == 6
    assert (await page(running))["total"] == 3
    publisher = context.event_publisher
    assert isinstance(publisher, CapturingPublisher)
    assert publisher.events == []


async def test_workload_profile_reuses_cache_until_expiry(running: RunningApp) -> None:
    context = running.context
    assert context.people_client is not None
    await context.people_client.aclose()
    timer, transport = Timer(), CountingTransport()
    context.people_client = PeopleClient(
        Settings(people_cache_ttl_seconds=30), transport=transport, cache_timer=timer
    )
    url = f"/instructors/{I1}/workload?week=2026-W49"
    first = await running.client.get(url)
    assert first.status_code == 200
    assert first.json()["max_hours_per_day"] == 6
    transport.overrides[PROFILE] = INSTRUCTORS[str(I1)] | {"max_hours_per_day": 1}
    second = await running.client.get(url)
    assert second.json() == first.json()
    assert transport.paths == [PROFILE]
    timer.value += 30
    third = await running.client.get(url)
    CONTRACT.assert_schema(third.json(), "Workload")
    assert third.json()["max_hours_per_day"] == 1
    assert third.json()["days"][1]["exceeded"] is True
    assert transport.paths == [PROFILE, PROFILE]


async def test_cached_parallel_creates_still_have_one_winner(running: RunningApp) -> None:
    context = running.context
    assert context.people_client is not None
    await context.people_client.aclose()
    transport = CountingTransport(delay=0.02)
    context.people_client = PeopleClient(Settings(), transport=transport)
    body = BODY | {"starts_at": "2026-12-10T07:00:00Z", "ends_at": "2026-12-10T08:00:00Z"}
    async with asyncio.timeout(3):
        responses = await asyncio.gather(
            running.client.post("/lessons", json=body), running.client.post("/lessons", json=body)
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    failure = next(response for response in responses if response.status_code == 409)
    CONTRACT.assert_error(failure, 409, "SCHEDULE_CONFLICT")
    assert len(transport.paths) == transport.max_in_flight == 3
