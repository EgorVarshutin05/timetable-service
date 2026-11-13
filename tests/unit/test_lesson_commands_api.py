"""Контракт POST и короткое замыкание BR-01/02/03 без PostgreSQL."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_lesson_service
from app.clock import AdjustableClock
from app.domain.lesson import LessonStatus
from app.domain.scheduling import Assessment, Issue
from app.errors import ApiError
from app.main import create_app
from app.models import Group, Lesson
from app.repositories import scheduling
from app.services.lessons import LessonService, PeopleSource
from tests.e2e.contract import ContractChecker
from tests.unit.scheduling_data import DRAFT, G1, L1

CONTRACT = ContractChecker.from_file(Path(__file__).resolve().parents[2] / "openapi.yaml")
BODY: dict[str, Any] = {
    "group_id": str(DRAFT.group_id),
    "instructor_id": str(DRAFT.instructor_id),
    "classroom_id": str(DRAFT.classroom_id),
    "format": "IN_PERSON",
    "starts_at": "2026-12-02T07:00:00Z",
    "ends_at": "2026-12-02T08:30:00Z",
}


@pytest.fixture
async def command_client() -> AsyncIterator[tuple[httpx.AsyncClient, MagicMock, AsyncMock]]:
    factory = MagicMock(spec=async_sessionmaker)
    people = AsyncMock(spec=PeopleSource)
    service = LessonService(
        cast(async_sessionmaker[AsyncSession], factory),
        cast(PeopleSource, people),
        AdjustableClock(),
    )
    app = create_app()
    app.dependency_overrides[get_lesson_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, factory, people


@pytest.mark.parametrize("path", ["/lessons", "/lessons/validate"])
@pytest.mark.parametrize(
    ("patch", "status", "code"),
    [
        ({"ends_at": "2026-12-02T07:29:00Z", "classroom_id": None}, 422, "VALIDATION_ERROR"),
        ({"ends_at": "2026-12-02T15:01:00Z"}, 422, "VALIDATION_ERROR"),
        ({"starts_at": "2026-12-02T07:00:00"}, 422, "VALIDATION_ERROR"),
        ({"starts_at": "1796194800"}, 422, "VALIDATION_ERROR"),
        ({"starts_at": 1796194800}, 400, "BAD_REQUEST"),
        ({"ends_at": {}}, 400, "BAD_REQUEST"),
        ({"classroom_id": None}, 422, "CLASSROOM_REQUIRED"),
        ({"format": "ONLINE"}, 422, "VALIDATION_ERROR"),
        ({"format": {}}, 400, "BAD_REQUEST"),
        ({"group_id": "bad-uuid"}, 400, "BAD_REQUEST"),
        ({"extra": True}, 422, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_input_never_calls_database_or_stub(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    path: str,
    patch: dict[str, Any],
    status: int,
    code: str,
) -> None:
    client, factory, people = command_client
    response = await client.post(path, json=BODY | patch)
    details = CONTRACT.assert_error(response, status, code)
    if code == "VALIDATION_ERROR":
        assert details["fields"]
    factory.assert_not_called()
    people.get_lesson_people.assert_not_awaited()


async def test_malformed_json_is_400(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
) -> None:
    client, factory, _ = command_client
    response = await client.post(
        "/lessons", content=b"{bad", headers={"content-type": "application/json"}
    )
    CONTRACT.assert_error(response, 400, "BAD_REQUEST")
    factory.assert_not_called()


@pytest.mark.parametrize("path", ["/lessons", "/lessons/validate"])
async def test_group_not_found_precedes_upstream(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    client, _, people = command_client
    monkeypatch.setattr(scheduling, "get_group", AsyncMock(return_value=None))
    response = await client.post(path, json=BODY)
    CONTRACT.assert_error(response, 422, "GROUP_NOT_FOUND")
    people.get_lesson_people.assert_not_awaited()


@pytest.mark.parametrize("path", ["/lessons", "/lessons/validate"])
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "INSTRUCTOR_NOT_FOUND"),
        (422, "CLASSROOM_NOT_FOUND"),
        (502, "UPSTREAM_UNAVAILABLE"),
        (504, "UPSTREAM_TIMEOUT"),
    ],
)
async def test_upstream_errors_unchanged(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    status: int,
    code: str,
) -> None:
    client, factory, people = command_client
    monkeypatch.setattr(
        scheduling, "get_group", AsyncMock(return_value=Group(id=G1, name="G", size=12))
    )
    people.get_lesson_people.side_effect = ApiError(status, code, "Upstream failed")
    response = await client.post(path, json=BODY)
    CONTRACT.assert_error(response, status, code)
    assert factory.call_count == 1  # Запись/транзакция блокировок не начиналась.


@pytest.mark.parametrize("dry_run", [False, True])
async def test_success_response_contract(dry_run: bool) -> None:
    service = AsyncMock(spec=LessonService)
    lesson = Lesson(
        id=L1,
        group_id=DRAFT.group_id,
        instructor_id=DRAFT.instructor_id,
        classroom_id=DRAFT.classroom_id,
        format=DRAFT.format,
        starts_at=DRAFT.starts_at,
        ends_at=DRAFT.ends_at,
        status=LessonStatus.PLANNED,
        rescheduled_from_id=None,
        cancel_reason=None,
        created_at=datetime(2026, 11, 30, tzinfo=UTC),
        updated_at=datetime(2026, 11, 30, tzinfo=UTC),
    )
    report = Assessment(
        (),
        (
            Issue(
                "INSTRUCTOR_UNAVAILABLE",
                "Предупреждение",
                {"instructor_id": str(DRAFT.instructor_id)},
            ),
        ),
    )
    service.execute.return_value = (None if dry_run else lesson, report)
    app = create_app()
    app.dependency_overrides[get_lesson_service] = lambda: cast(LessonService, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/lessons/validate" if dry_run else "/lessons", json=BODY)
    assert response.status_code == (200 if dry_run else 201)
    CONTRACT.assert_schema(response.json(), "ValidationResult" if dry_run else "LessonWithWarnings")
    assert response.json()["warnings"][0]["code"] == "INSTRUCTOR_UNAVAILABLE"


async def test_validate_returns_all_violations_as_200() -> None:
    service = AsyncMock(spec=LessonService)
    report = Assessment(
        (
            Issue("CLASSROOM_TOO_SMALL", "Мало мест", {"capacity": 10, "group_size": 12}),
            Issue("SCHEDULE_CONFLICT", "Конфликт", {"conflicts": []}),
        ),
        (),
    )
    service.execute.return_value = (None, report)
    app = create_app()
    app.dependency_overrides[get_lesson_service] = lambda: cast(LessonService, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/lessons/validate", json=BODY)
    assert response.status_code == 200
    CONTRACT.assert_schema(response.json(), "ValidationResult")
    assert response.json()["ok"] is False
    assert [issue["code"] for issue in response.json()["violations"]] == [
        "CLASSROOM_TOO_SMALL",
        "SCHEDULE_CONFLICT",
    ]
