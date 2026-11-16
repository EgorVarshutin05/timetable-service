"""FR-17: fake monotonic timer, bounded cache and concurrent cold/hot lookups."""

import asyncio
from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app.clients.cache import AsyncTTLCache
from app.clients.people import PeopleClient
from app.domain.lesson import LessonFormat
from app.errors import ApiError
from app.settings import Settings
from tests.unit.test_people_client import (
    AVAILABILITY,
    C1,
    CLASSROOM,
    CLASSROOMS,
    I1,
    INSTRUCTORS,
    PROFILE,
    CountingTransport,
)


@dataclass
class Timer:
    value: float = 100

    def __call__(self) -> float:
        return self.value


@pytest.mark.parametrize("ttl", [0.5, 30.0, 60.0])
async def test_all_endpoints_hit_expire_and_refresh(ttl: float) -> None:
    timer, transport = Timer(), CountingTransport()
    async with PeopleClient(
        Settings(people_cache_ttl_seconds=ttl), transport=transport, cache_timer=timer
    ) as client:
        first = await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        timer.value += ttl - 0.01
        second = await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        assert second == first
        assert len(transport.paths) == 3
        # Включительная граница expiry; TTL считается от успешного конца запроса.
        timer.value += 0.01
        await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        assert [transport.paths.count(path) for path in [PROFILE, AVAILABILITY, CLASSROOM]] == [
            2,
            2,
            2,
        ]


async def test_ttl_starts_after_success_not_at_request_start() -> None:
    timer = Timer()

    async def response(request: httpx.Request) -> httpx.Response:
        timer.value += 20
        return httpx.Response(200, json=INSTRUCTORS[str(I1)])

    async with PeopleClient(
        Settings(people_cache_ttl_seconds=10),
        transport=httpx.MockTransport(response),
        cache_timer=timer,
    ) as client:
        first = await client.get_instructor(I1)
        timer.value += 9
        assert await client.get_instructor(I1) is first
        assert timer.value == 129


async def test_cache_disabled_does_not_reuse_values() -> None:
    transport = CountingTransport()
    async with PeopleClient(Settings(people_cache_ttl_seconds=0), transport=transport) as client:
        for _ in range(2):
            await client.get_lesson_people(
                instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
            )
    assert len(transport.paths) == 6


@pytest.mark.parametrize("online", [False, True])
async def test_same_keys_single_flight_preserves_different_endpoint_parallelism(
    online: bool,
) -> None:
    transport = CountingTransport(delay=0.02)
    async with PeopleClient(Settings(), transport=transport) as client:
        values = await asyncio.gather(
            *[
                client.get_lesson_people(
                    instructor_id=I1,
                    lesson_format=LessonFormat.ONLINE if online else LessonFormat.IN_PERSON,
                    classroom_id=None if online else C1,
                )
                for _ in range(20)
            ]
        )
        assert len(transport.paths) == transport.max_in_flight == (2 if online else 3)
        assert all(value == values[0] for value in values)
        await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        assert len(transport.paths) == 3
    assert transport.in_flight == 0


async def test_different_ids_and_client_instances_are_isolated() -> None:
    i2 = UUID(int=I1.int + 1)
    transport = CountingTransport(delay=0.01)
    async with PeopleClient(Settings(), transport=transport) as client:
        a, b = await asyncio.gather(client.get_instructor(I1), client.get_instructor(i2))
        assert a.id == I1
        assert b.id == i2
        assert transport.max_in_flight == 2
    other = CountingTransport()
    async with PeopleClient(Settings(), transport=other) as client:
        await client.get_instructor(I1)
    assert other.paths == [PROFILE]


@pytest.mark.parametrize("failure", [404, 500, httpx.ReadTimeout, httpx.ConnectError])
@pytest.mark.parametrize("endpoint", [PROFILE, AVAILABILITY, CLASSROOM])
async def test_failures_are_never_cached(
    failure: int | type[httpx.RequestError], endpoint: str
) -> None:
    transport = CountingTransport(failures={endpoint: failure})
    async with PeopleClient(Settings(), transport=transport) as client:

        async def lookup() -> object:
            if endpoint == PROFILE:
                return await client.get_instructor(I1)
            if endpoint == AVAILABILITY:
                return await client.get_availability(I1)
            return await client.get_classroom(C1)

        for _ in range(2):
            with pytest.raises(ApiError):
                await lookup()
        assert transport.paths == [endpoint, endpoint]
        transport.failures.clear()
        await lookup()
        await lookup()
        assert transport.paths == [endpoint] * 3


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {
            "id": str(UUID(int=I1.int + 1)),
            "full_name": "Wrong",
            "max_hours_per_day": 6,
            "timezone": "UTC",
        },
    ],
)
async def test_invalid_success_payload_or_wrong_id_is_not_cached(bad: dict[str, object]) -> None:
    transport = CountingTransport(overrides={PROFILE: bad})
    async with PeopleClient(Settings(), transport=transport) as client:
        for _ in range(2):
            with pytest.raises(ApiError, match="некорректный"):
                await client.get_instructor(I1)
        transport.overrides.clear()
        assert (await client.get_instructor(I1)).id == I1
    assert transport.paths == [PROFILE] * 3


async def test_clear_cache_invalidates_all_types_and_close_rejects_hits() -> None:
    transport = CountingTransport()
    client = PeopleClient(Settings(), transport=transport)
    async with client:
        await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        transport.overrides[PROFILE] = INSTRUCTORS[str(I1)] | {"max_hours_per_day": 2}
        transport.overrides[CLASSROOM] = CLASSROOMS[str(C1)] | {"capacity": 1}
        assert (await client.get_instructor(I1)).max_hours_per_day == Decimal(6)
        client.clear_cache()
        result = await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        assert result.instructor.max_hours_per_day == Decimal(2)
        assert result.classroom is not None
        assert result.classroom.capacity == 1
        assert len(transport.paths) == 6
        attribute = "timezone"
        with pytest.raises(FrozenInstanceError):
            setattr(result.instructor, attribute, "UTC")
    for lookup in [
        client.get_instructor(I1),
        client.get_availability(I1),
        client.get_classroom(C1),
    ]:
        with pytest.raises(RuntimeError, match="закрыт"):
            await lookup


async def test_clear_during_flight_prevents_stale_repopulation() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def response(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return httpx.Response(200, json=INSTRUCTORS[str(I1)] | {"max_hours_per_day": calls})

    async with PeopleClient(Settings(), transport=httpx.MockTransport(response)) as client:
        task = asyncio.create_task(client.get_instructor(I1))
        await started.wait()
        client.clear_cache()
        release.set()
        assert (await task).max_hours_per_day == 1
        assert (await client.get_instructor(I1)).max_hours_per_day == 2
        assert (await client.get_instructor(I1)).max_hours_per_day == 2
        assert calls == 2


async def test_cancelled_leader_does_not_cancel_waiter_or_poison_key() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def response(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return httpx.Response(200, json=INSTRUCTORS[str(I1)])

    async with PeopleClient(Settings(), transport=httpx.MockTransport(response)) as client:
        leader = asyncio.create_task(client.get_instructor(I1))
        await started.wait()
        waiter = asyncio.create_task(client.get_instructor(I1))
        await asyncio.sleep(0)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        async with asyncio.timeout(1):
            assert (await waiter).id == I1
        assert (await client.get_instructor(I1)).id == I1
        assert calls == 2


async def test_cancelled_waiter_does_not_cancel_owner() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def response(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json=INSTRUCTORS[str(I1)])

    async with PeopleClient(Settings(), transport=httpx.MockTransport(response)) as client:
        owner = asyncio.create_task(client.get_instructor(I1))
        await started.wait()
        waiter = asyncio.create_task(client.get_instructor(I1))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert (await owner).id == I1
        assert (await client.get_instructor(I1)).id == I1


async def test_bounded_lru_and_expired_entries() -> None:
    timer = Timer()
    cache = AsyncTTLCache[str](10, timer, maxsize=2)
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return str(calls)

    a, b, c = UUID(int=1), UUID(int=2), UUID(int=3)
    assert await cache.get(a, fetch) == "1"
    assert await cache.get(b, fetch) == "2"
    assert await cache.get(a, fetch) == "1"
    assert await cache.get(c, fetch) == "3"  # b — LRU
    assert await cache.get(b, fetch) == "4"
    timer.value += 10
    assert await cache.get(b, fetch) == "5"


async def test_clear_from_another_thread() -> None:
    transport = CountingTransport()
    async with PeopleClient(Settings(), transport=transport) as client:
        await client.get_instructor(I1)
        await asyncio.to_thread(client.clear_cache)
        await client.get_instructor(I1)
        assert transport.paths == [PROFILE, PROFILE]


@pytest.mark.parametrize("ttl", [-1, float("nan"), float("inf"), 3601])
def test_invalid_cache_settings(ttl: float) -> None:
    with pytest.raises(ValidationError):
        Settings(people_cache_ttl_seconds=ttl)
