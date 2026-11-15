"""FR-14: настоящая атомарность, внутрисерийные правила и гонки PostgreSQL."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models import Lesson
from tests.unit.event_helpers import CapturingPublisher
from tests.unit.scheduling_data import C2, L1
from tests.unit.test_lesson_commands_api import BODY as SINGLE_BODY
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_series import BODY
from tests.unit.test_wp01_postgres import Database
from tests.unit.test_wp01_postgres import database as database
from tests.unit.test_wp03_postgres import RunningApp
from tests.unit.test_wp03_postgres import running as running
from tests.unit.test_wp04_postgres import gated_people


def publisher(running: RunningApp) -> CapturingPublisher:
    result = running.context.event_publisher
    assert isinstance(result, CapturingPublisher)
    return result


async def count_rows(database: Database) -> int:
    async with database.factory() as session:
        result = await session.scalar(select(func.count()).select_from(Lesson))
        assert result is not None
        return result


async def test_series_success_gets_and_single_parallel_people_batch(
    running: RunningApp, database: Database
) -> None:
    response = await running.client.post("/lesson-series", json=BODY)
    assert response.status_code == 201, response.text
    data = response.json()
    CONTRACT.assert_schema(data, "LessonSeriesResult")
    assert UUID(data["series_id"]).version == 4
    assert [item["lesson"]["starts_at"] for item in data["items"]] == [
        "2026-12-10T07:00:00Z",
        "2026-12-17T07:00:00Z",
        "2026-12-24T07:00:00Z",
    ]
    assert len({item["lesson"]["id"] for item in data["items"]}) == 3
    for item in data["items"]:
        lesson = item["lesson"]
        assert item["warnings"] == []
        assert lesson["status"] == "PLANNED"
        assert lesson["series_id"] == data["series_id"]
        assert lesson["created_at"] == lesson["updated_at"] == "2026-11-30T03:00:00Z"
        assert lesson["rescheduled_from_id"] is None
        assert lesson["cancel_reason"] is None
        fetched = await running.client.get(f"/lessons/{lesson['id']}")
        CONTRACT.assert_schema(fetched.json(), "Lesson")
        assert fetched.json() == lesson
    assert await count_rows(database) == 11
    assert publisher(running).events == []
    assert running.context.people_client is not None
    # Production client использует исходный тестовый транспорт WP-03.
    # Один batch доказывается общим request counter, а не количеством занятий.
    gate = await gated_people(running, 3)
    second_body = BODY | {"first_starts_at": "2027-03-01T07:00:00Z", "count": 5}
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        second = tasks.create_task(running.client.post("/lesson-series", json=second_body))
        await gate.arrived.wait()
        gate.release.set()
    assert second.result().status_code == 201
    assert gate.started == 3
    assert gate.max_in_flight == 3


@pytest.mark.parametrize(("count", "duration"), [(1, 30), (50, 480)])
async def test_series_boundary_sizes_and_durations(
    running: RunningApp, database: Database, count: int, duration: int
) -> None:
    response = await running.client.post(
        "/lesson-series",
        json=BODY
        | {
            "count": count,
            "duration_minutes": duration,
        },
    )
    assert response.status_code == 201, response.text
    CONTRACT.assert_schema(response.json(), "LessonSeriesResult")
    assert len(response.json()["items"]) == count
    assert await count_rows(database) == 8 + count
    assert publisher(running).events == []


async def test_late_conflict_rolls_back_all_previous_occurrences(
    running: RunningApp, database: Database
) -> None:
    before = (await running.client.get("/lessons")).json()
    body = BODY | {
        "group_id": "9a000000-0000-4000-8000-000000000003",
        "classroom_id": None,
        "format": "ONLINE",
        "weekdays": ["SUN", "MON", "TUE"],
        "first_starts_at": "2026-11-29T05:00:00Z",
        "count": 3,
    }
    response = await running.client.post("/lesson-series", json=body)
    details = CONTRACT.assert_error(response, 409, "SCHEDULE_CONFLICT")
    assert details["occurrence_index"] == 2
    assert details["conflicts"] == [
        {
            "resource": "instructor",
            "lesson_id": str(L1),
            "overlap": {
                "starts_at": "2026-12-01T05:00:00Z",
                "ends_at": "2026-12-01T06:30:00Z",
            },
        }
    ]
    assert await count_rows(database) == 8
    assert (await running.client.get("/lessons")).json() == before
    assert publisher(running).events == []


async def test_capacity_failure_has_index_and_no_series_rows(
    running: RunningApp, database: Database
) -> None:
    result = await running.client.post("/lesson-series", json=BODY | {"classroom_id": str(C2)})
    assert CONTRACT.assert_error(result, 409, "CLASSROOM_TOO_SMALL") == {
        "capacity": 10,
        "group_size": 12,
        "occurrence_index": 0,
    }
    assert await count_rows(database) == 8
    assert publisher(running).events == []


async def test_series_accumulates_workload_in_the_same_local_day(running: RunningApp) -> None:
    response = await running.client.post(
        "/lesson-series",
        json=BODY
        | {
            "classroom_id": None,
            "format": "ONLINE",
            "weekdays": ["THU", "FRI"],
            "count": 2,
            "first_starts_at": "2026-12-10T17:00:00Z",
            "duration_minutes": 480,
        },
    )
    assert response.status_code == 201, response.text
    first, second = response.json()["items"]
    assert all(w["code"] != "WORKLOAD_SOFT_LIMIT" for w in first["warnings"])
    workload = [w for w in second["warnings"] if w["code"] == "WORKLOAD_SOFT_LIMIT"]
    assert len(workload) == 1
    assert workload[0]["details"] == {"date": "2026-12-11", "max_hours": 6.0, "planned_hours": 8.0}
    assert publisher(running).events == []


@pytest.mark.parametrize("second_start", ["2026-12-10T07:00:00Z", "2026-12-17T07:00:00Z"])
async def test_concurrent_overlapping_series_have_one_atomic_winner(
    running: RunningApp, database: Database, second_start: str
) -> None:
    gate = await gated_people(running, 6)
    body_b = BODY | {"count": 2, "first_starts_at": second_start}
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        a = tasks.create_task(running.client.post("/lesson-series", json=BODY))
        b = tasks.create_task(running.client.post("/lesson-series", json=body_b))
        await gate.arrived.wait()
        gate.release.set()
    responses = [a.result(), b.result()]
    assert sorted(r.status_code for r in responses) == [201, 409]
    winner = next(r for r in responses if r.status_code == 201)
    loser = next(r for r in responses if r.status_code == 409)
    details = CONTRACT.assert_error(loser, 409, "SCHEDULE_CONFLICT")
    expected_index = (
        1 if a.result().status_code == 409 and second_start.endswith("17T07:00:00Z") else 0
    )
    assert details["occurrence_index"] == expected_index
    assert await count_rows(database) == 8 + len(winner.json()["items"])
    async with database.factory() as session:
        assert await session.scalar(select(func.count(func.distinct(Lesson.series_id)))) == 1
    assert publisher(running).events == []


async def test_series_and_single_create_share_the_resource_lock_protocol(
    running: RunningApp, database: Database
) -> None:
    gate = await gated_people(running, 6)
    body = SINGLE_BODY | {"starts_at": BODY["first_starts_at"], "ends_at": "2026-12-10T08:30:00Z"}
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        series = tasks.create_task(running.client.post("/lesson-series", json=BODY))
        single = tasks.create_task(running.client.post("/lessons", json=body))
        await gate.arrived.wait()
        gate.release.set()
    assert sorted([series.result().status_code, single.result().status_code]) == [201, 409]
    assert await count_rows(database) == (11 if series.result().status_code == 201 else 9)
    assert publisher(running).events == []


async def test_series_membership_survives_patch_and_reschedule(running: RunningApp) -> None:
    created = await running.client.post("/lesson-series", json=BODY | {"count": 1})
    assert created.status_code == 201
    data = created.json()
    lesson_id = data["items"][0]["lesson"]["id"]
    assert publisher(running).events == []
    patched = await running.client.patch(
        f"/lessons/{lesson_id}", json={"ends_at": "2026-12-10T08:00:00Z"}
    )
    assert patched.status_code == 200
    assert patched.json()["lesson"]["series_id"] == data["series_id"]
    moved = await running.client.post(
        f"/lessons/{lesson_id}/reschedule",
        json={
            "starts_at": "2026-12-11T07:00:00Z",
            "ends_at": "2026-12-11T08:30:00Z",
        },
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["lesson"]["series_id"] == data["series_id"]
    assert len(publisher(running).events) == 1  # Только reschedule, не создание серии.


async def test_single_and_seed_lessons_return_null_series_id(running: RunningApp) -> None:
    seed = await running.client.get(f"/lessons/{L1}")
    assert seed.json()["series_id"] is None
    created = await running.client.post("/lessons", json=SINGLE_BODY)
    assert created.status_code == 201
    assert created.json()["lesson"]["series_id"] is None
    assert publisher(running).events == []
