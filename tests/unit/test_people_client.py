"""FR-12/NFR-07: HTTPX с транспортом в памяти, без сети, БД и брокера."""

import asyncio
import json
import runpy
from collections.abc import Callable
from datetime import time
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.clients.people import PeopleClient
from app.domain.lesson import LessonFormat
from app.errors import ApiError
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "stubs/people-stub/fixtures.json"
RAW = json.loads(FIXTURES.read_text(encoding="utf-8"))
INSTRUCTORS = {row["id"]: row for row in RAW["instructors"]}
CLASSROOMS = {row["id"]: row for row in RAW["classrooms"]}
I1 = UUID("1b000000-0000-4000-8000-000000000001")
C1 = UUID("2c000000-0000-4000-8000-000000000001")
I4 = UUID("1b000000-0000-4000-8000-000000000004")
C4 = UUID("2c000000-0000-4000-8000-000000000004")
PROFILE = f"/instructors/{I1}"
AVAILABILITY = f"{PROFILE}/availability"
CLASSROOM = f"/classrooms/{C1}"
Failure = int | type[httpx.RequestError]


class CountingTransport(httpx.AsyncBaseTransport):
    """Задержка и счётчик охватывают весь запрос, как track() исходной заглушки."""

    def __init__(
        self,
        *,
        delay: float = 0,
        failures: dict[str, Failure] | None = None,
        delays: dict[str, float] | None = None,
        overrides: dict[str, object] | None = None,
    ) -> None:
        self.delay = delay
        self.failures = failures or {}
        self.delays = delays or {}
        self.overrides = overrides or {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.paths: list[str] = []
        self.urls: list[httpx.URL] = []
        self.timeouts: list[object] = []
        self.closed = False
        self.three_started = asyncio.Event()

    async def aclose(self) -> None:
        self.closed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.urls.append(request.url)
        self.timeouts.append(request.extensions.get("timeout"))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight == 3:
            self.three_started.set()
        try:
            await asyncio.sleep(self.delays.get(request.url.path, self.delay))
            failure = self.failures.get(request.url.path)
            if isinstance(failure, int):
                return httpx.Response(failure, json={"error": "injected"})
            if failure is not None:
                raise failure("Injected failure", request=request)
            if request.url.path in self.overrides:
                return httpx.Response(200, json=self.overrides[request.url.path])
            parts = request.url.path.strip("/").split("/")
            if parts[0] == "classrooms":
                classroom = CLASSROOMS.get(parts[1])
                return httpx.Response(200, json=classroom) if classroom else httpx.Response(404)
            instructor = INSTRUCTORS.get(parts[1])
            if instructor is None:
                return httpx.Response(404)
            if len(parts) == 3:
                if instructor.get("fail_availability", False):
                    return httpx.Response(500)
                return httpx.Response(
                    200,
                    json={
                        "instructor_id": instructor["id"],
                        "timezone": instructor["timezone"],
                        "windows": instructor["windows"],
                    },
                )
            return httpx.Response(
                200,
                json={
                    key: instructor[key]
                    for key in ("id", "full_name", "max_hours_per_day", "timezone")
                },
            )
        finally:
            self.in_flight -= 1


async def test_domain_values_and_http_settings() -> None:
    transport = CountingTransport()
    settings = Settings(stub_base_url="http://people.example", stub_timeout_ms=1750)
    async with PeopleClient(settings, transport=transport) as client:
        people = await client.get_lesson_people(
            instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
        )
        assert people.instructor.max_hours_per_day == Decimal("6")
        assert people.instructor.timezone == "Asia/Yekaterinburg"
        assert people.availability.windows[0].starts_at == time(9)
        assert people.classroom is not None
        assert people.classroom.capacity == 30
        was_open = not client.is_closed
        assert was_open
    assert client.is_closed
    assert transport.closed
    assert transport.paths == [PROFILE, AVAILABILITY, CLASSROOM]
    assert str(transport.urls[0]) == f"http://people.example{PROFILE}"
    assert transport.timeouts == [{"connect": 1.75, "read": 1.75, "write": 1.75, "pool": 1.75}] * 3


@pytest.mark.parametrize("lesson_format", [LessonFormat.IN_PERSON, LessonFormat.ONLINE])
async def test_async_case(lesson_format: LessonFormat) -> None:
    """300 мс на вызов: измеряем обе стратегии одинаковым транспортом."""
    classroom_id = C1 if lesson_format == LessonFormat.IN_PERSON else None
    sequential_transport = CountingTransport(delay=0.3)
    async with PeopleClient(Settings(), transport=sequential_transport) as client:
        start = perf_counter()
        await client.get_instructor(I1)
        await client.get_availability(I1)
        if classroom_id is not None:
            await client.get_classroom(classroom_id)
        sequential = perf_counter() - start
    parallel_transport = CountingTransport(delay=0.3)
    async with PeopleClient(Settings(), transport=parallel_transport) as client:
        start = perf_counter()
        result = await client.get_lesson_people(
            instructor_id=I1, lesson_format=lesson_format, classroom_id=classroom_id
        )
        parallel = perf_counter() - start
    count = 3 if classroom_id is not None else 2
    assert sequential_transport.max_in_flight == 1
    assert parallel_transport.max_in_flight == count
    assert parallel_transport.in_flight == 0
    assert len(parallel_transport.paths) == count
    assert (result.classroom is None) == (classroom_id is None)
    assert sequential >= count * 0.3
    # Относительный запас допускает планировщик CI; сам параллелизм доказывает счётчик.
    assert parallel < sequential * 0.8
    print(
        json.dumps(
            {
                "format": lesson_format.value,
                "sequential_ms": round(sequential * 1000, 2),
                "parallel_ms": round(parallel * 1000, 2),
                "speedup": round(sequential / parallel, 2),
                "max_in_flight": count,
            }
        )
    )


@pytest.mark.parametrize("path", [PROFILE, AVAILABILITY, CLASSROOM])
@pytest.mark.parametrize("status", [404, 500, 503])
async def test_status_mapping(path: str, status: int) -> None:
    transport = CountingTransport(failures={path: status})
    async with PeopleClient(Settings(), transport=transport) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_lesson_people(
                instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
            )
    expected = "CLASSROOM_NOT_FOUND" if path == CLASSROOM else "INSTRUCTOR_NOT_FOUND"
    assert caught.value.status_code == (422 if status == 404 else 502)
    assert caught.value.code == (expected if status == 404 else "UPSTREAM_UNAVAILABLE")
    assert transport.in_flight == 0


@pytest.mark.parametrize("entity", ["instructor", "classroom"])
async def test_i5_c5_timeout_mapping(entity: str) -> None:
    entity_id = UUID(
        f"{'1b' if entity == 'instructor' else '2c'}000000-0000-4000-8000-000000000005"
    )
    path = f"/{'instructors' if entity == 'instructor' else 'classrooms'}/{entity_id}"
    # Custom transport сам возбуждает TimeoutException; HTTPX не оборачивает его sleep таймером.
    transport = CountingTransport(failures={path: httpx.ReadTimeout})
    async with PeopleClient(Settings(stub_timeout_ms=20), transport=transport) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_lesson_people(
                instructor_id=entity_id if entity == "instructor" else I1,
                lesson_format=LessonFormat.IN_PERSON,
                classroom_id=entity_id if entity == "classroom" else C1,
            )
    assert caught.value.status_code == 504
    assert caught.value.code == "UPSTREAM_TIMEOUT"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadError])
async def test_network_error_mapping(error_type: type[httpx.RequestError]) -> None:
    transport = CountingTransport(failures={PROFILE: error_type})
    async with PeopleClient(Settings(), transport=transport) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_instructor(I1)
    assert caught.value.status_code == 502
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"


async def test_i6_available_profile_does_not_hide_failed_availability() -> None:
    i6 = UUID("1b000000-0000-4000-8000-000000000006")
    transport = CountingTransport()
    async with PeopleClient(Settings(), transport=transport) as client:
        assert (await client.get_instructor(i6)).id == i6
        with pytest.raises(ApiError) as caught:
            await client.get_lesson_people(instructor_id=i6, lesson_format=LessonFormat.ONLINE)
    assert caught.value.status_code == 502
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    assert f"/instructors/{i6}/availability" in transport.paths


@pytest.mark.parametrize("profile_delay", [0.0, 0.02])
async def test_error_precedence_does_not_depend_on_completion_order(profile_delay: float) -> None:
    transport = CountingTransport(
        failures={PROFILE: 404, AVAILABILITY: 500, CLASSROOM: 404},
        delays={PROFILE: profile_delay, AVAILABILITY: 0.01},
    )
    async with PeopleClient(Settings(), transport=transport) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_lesson_people(
                instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
            )
    assert caught.value.code == "INSTRUCTOR_NOT_FOUND"
    assert transport.in_flight == 0
    assert len(transport.paths) == 3


async def test_cancellation_drains_in_flight_requests() -> None:
    transport = CountingTransport(delay=60)
    async with PeopleClient(Settings(), transport=transport) as client:
        task = asyncio.create_task(
            client.get_lesson_people(
                instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
            )
        )
        async with asyncio.timeout(1):
            await transport.three_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.in_flight == 0


@pytest.mark.parametrize("payload", [{}, {"id": "bad"}, {"timezone": "Not/AZone"}])
async def test_malformed_profile_is_502(payload: dict[str, object]) -> None:
    transport = CountingTransport(overrides={PROFILE: payload})
    async with PeopleClient(Settings(), transport=transport) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_instructor(I1)
    assert caught.value.status_code == 502


@pytest.mark.parametrize(
    ("path", "patch"),
    [
        (PROFILE, {"timezone": "Not/AZone"}),
        (PROFILE, {"max_hours_per_day": 0}),
        (PROFILE, {"id": str(C1)}),
        (CLASSROOM, {"capacity": 0}),
        (AVAILABILITY, {"instructor_id": str(C1)}),
        (AVAILABILITY, {"windows": [{"weekday": 1, "from": "18:00", "to": "09:00"}]}),
    ],
)
async def test_invalid_upstream_values_are_502(path: str, patch: dict[str, object]) -> None:
    if path == PROFILE:
        payload = {
            key: INSTRUCTORS[str(I1)][key]
            for key in ("id", "full_name", "max_hours_per_day", "timezone")
        }
    elif path == CLASSROOM:
        payload = dict(CLASSROOMS[str(C1)])
    else:
        payload = {
            "instructor_id": str(I1),
            "timezone": "Asia/Yekaterinburg",
            "windows": INSTRUCTORS[str(I1)]["windows"],
        }
    payload.update(patch)
    transport = CountingTransport(overrides={path: payload})
    async with PeopleClient(Settings(), transport=transport) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_lesson_people(
                instructor_id=I1, lesson_format=LessonFormat.IN_PERSON, classroom_id=C1
            )
    assert caught.value.status_code == 502
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"


@pytest.mark.parametrize(
    ("lesson_format", "classroom_id"),
    [
        (LessonFormat.IN_PERSON, None),
        (LessonFormat.ONLINE, C1),
    ],
)
async def test_invalid_call_requires_prior_br02_validation(
    lesson_format: LessonFormat, classroom_id: UUID | None
) -> None:
    transport = CountingTransport()
    async with PeopleClient(Settings(), transport=transport) as client:
        with pytest.raises(ValueError, match="BR-02"):
            await client.get_lesson_people(
                instructor_id=I1, lesson_format=lesson_format, classroom_id=classroom_id
            )
    assert transport.paths == []


@pytest.fixture
def original_stub() -> FastAPI:
    namespace = runpy.run_path(str(ROOT / "stubs/people-stub/app.py"))
    factory = cast(Callable[[Path, dict[str, str]], FastAPI], namespace["create_app"])
    return factory(
        FIXTURES,
        {
            "STUB_LATENCY_MS": "10",
            "STUB_FAIL_IDS": f"{I4},{C4}",
        },
    )


@pytest.mark.parametrize("lesson_format", [LessonFormat.IN_PERSON, LessonFormat.ONLINE])
async def test_original_stub_stats_over_asgi(
    original_stub: FastAPI, lesson_format: LessonFormat
) -> None:
    transport = httpx.ASGITransport(app=original_stub)
    async with PeopleClient(Settings(), transport=transport) as client:
        await client.get_lesson_people(
            instructor_id=I1,
            lesson_format=lesson_format,
            classroom_id=C1 if lesson_format == LessonFormat.IN_PERSON else None,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=original_stub), base_url="http://stub"
    ) as admin:
        response = await admin.get("/__stub/stats")
        stats = response.json()
        count = 3 if lesson_format == LessonFormat.IN_PERSON else 2
        assert stats["total"] == count
        assert stats["max_in_flight"] == count
        assert stats["in_flight"] == 0
        await admin.delete("/__stub/stats")
        assert (await admin.get("/__stub/stats")).json()["total"] == 0


@pytest.mark.parametrize(
    ("instructor_id", "classroom_id"),
    [
        (UUID("1b000000-0000-4000-8000-000000000004"), C1),
        (UUID("1b000000-0000-4000-8000-000000000006"), C1),
        (I1, UUID("2c000000-0000-4000-8000-000000000004")),
    ],
)
async def test_i4_i6_c4_against_original_stub(
    original_stub: FastAPI, instructor_id: UUID, classroom_id: UUID
) -> None:
    async with PeopleClient(Settings(), transport=httpx.ASGITransport(app=original_stub)) as client:
        with pytest.raises(ApiError) as caught:
            await client.get_lesson_people(
                instructor_id=instructor_id,
                lesson_format=LessonFormat.IN_PERSON,
                classroom_id=classroom_id,
            )
    assert caught.value.status_code == 502
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
