"""FR-03/05: чистые параметры, слияние PATCH и API без PostgreSQL."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_lesson_service
from app.api.schemas import LessonPatch
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.listing import LessonFilters, validate_filters
from app.domain.scheduling import InvalidLesson, ensure_editable
from app.errors import ApiError
from app.main import create_app
from app.models import Lesson
from app.repositories import listing, scheduling
from app.services.lessons import LessonService
from tests.unit.scheduling_data import C1, DRAFT, G1, I1, L1
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_commands_api import command_client as command_client


def row(status: LessonStatus = LessonStatus.PLANNED) -> Lesson:
    return Lesson(
        id=L1,
        group_id=G1,
        instructor_id=I1,
        classroom_id=C1,
        format=DRAFT.format,
        starts_at=DRAFT.starts_at,
        ends_at=DRAFT.ends_at,
        status=status,
        rescheduled_from_id=None,
        cancel_reason=None,
        created_at=datetime(2026, 11, 20, tzinfo=UTC),
        updated_at=datetime(2026, 11, 20, tzinfo=UTC),
    )


@pytest.mark.parametrize("status", list(LessonStatus))
def test_editable_status_rule(status: LessonStatus) -> None:
    if status == LessonStatus.PLANNED:
        ensure_editable(status)
    else:
        with pytest.raises(InvalidLesson) as error:
            ensure_editable(status)
        assert error.value.issue.details == {"current_status": status.value}


@pytest.mark.parametrize(
    "filters",
    [
        LessonFilters(limit=0),
        LessonFilters(limit=201),
        LessonFilters(offset=-1),
        LessonFilters(starts_from=DRAFT.starts_at, starts_to=DRAFT.starts_at),
        LessonFilters(starts_from=DRAFT.ends_at, starts_to=DRAFT.starts_at),
        LessonFilters(starts_from=DRAFT.starts_at.replace(tzinfo=None)),
    ],
)
def test_list_parameter_rules(filters: LessonFilters) -> None:
    with pytest.raises(InvalidLesson) as error:
        validate_filters(filters)
    assert error.value.issue.code == "VALIDATION_ERROR"
    assert error.value.issue.details["fields"]


def test_patch_fields_set_distinguishes_null_and_omission() -> None:
    partial = LessonPatch.model_validate({"ends_at": "2026-12-02T08:00:00Z"})
    assert partial.model_fields_set == {"ends_at"}
    merged = partial.apply(DRAFT)
    assert merged.classroom_id == C1
    assert merged.starts_at == DRAFT.starts_at
    explicit = LessonPatch.model_validate({"format": "ONLINE", "classroom_id": None})
    assert explicit.model_fields_set == {"format", "classroom_id"}
    merged = explicit.apply(DRAFT)
    assert merged.classroom_id is None
    assert merged.format == LessonFormat.ONLINE
    assert merged.group_id == DRAFT.group_id


async def test_list_query_combines_filters_and_paginates() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = 8
    result = MagicMock()
    result.all.return_value = [row()]
    session.scalars.return_value = result
    filters = LessonFilters(
        G1, I1, C1, LessonStatus.PLANNED, DRAFT.starts_at, DRAFT.ends_at, limit=1, offset=2
    )
    items, total = await listing.get_page(cast(AsyncSession, session), filters)
    assert len(items) == 1
    assert total == 8
    count_sql = str(session.scalar.await_args.args[0])
    page_sql = str(session.scalars.await_args.args[0])
    assert "LIMIT" not in count_sql
    assert "OFFSET" not in count_sql
    assert "ORDER BY lessons.starts_at, lessons.id" in page_sql
    assert "LIMIT" in page_sql
    assert "OFFSET" in page_sql
    params = session.scalars.await_args.args[0].compile().params
    assert G1 in params.values()
    assert I1 in params.values()
    assert C1 in params.values()
    assert DRAFT.starts_at in params.values()
    assert DRAFT.ends_at in params.values()


async def test_union_locks_are_globally_sorted_and_deduplicated() -> None:
    changed = replace(
        DRAFT,
        instructor_id=UUID("1b000000-0000-4000-8000-000000000003"),
        classroom_id=None,
        format=LessonFormat.ONLINE,
    )
    session = AsyncMock(spec=AsyncSession)
    await scheduling.lock_resources(cast(AsyncSession, session), DRAFT, changed)
    expected = sorted(
        set(scheduling.resource_lock_keys(DRAFT)) | set(scheduling.resource_lock_keys(changed))
    )
    assert [call.args[1]["key"] for call in session.execute.await_args_list] == expected


@pytest.fixture
async def list_client() -> AsyncIterator[tuple[httpx.AsyncClient, AsyncMock]]:
    service = AsyncMock(spec=LessonService)
    service.list_lessons.return_value = ([row()], 8)
    app = create_app()
    app.dependency_overrides[get_lesson_service] = lambda: cast(LessonService, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, service


async def test_list_defaults_and_date_aliases(
    list_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    client, service = list_client
    response = await client.get("/lessons")
    assert response.status_code == 200
    CONTRACT.assert_schema(response.json(), "LessonPage")
    filters = service.list_lessons.await_args.args[0]
    assert filters.limit == 50
    assert filters.offset == 0
    response = await client.get(
        "/lessons", params={"from": "2026-12-02T12:00:00+05:00", "to": "2026-12-02T13:30:00+05:00"}
    )
    assert response.status_code == 200
    filters = service.list_lessons.await_args.args[0]
    assert filters.starts_from == DRAFT.starts_at
    assert filters.starts_to == DRAFT.ends_at


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "201"},
        {"limit": "0"},
        {"limit": "x"},
        {"offset": "-1"},
        {"group_id": "bad"},
        {"status": "bad"},
        {"from": "2026-12-02T07:00:00"},
        {"from": "1796194800"},
        {"from": "2026-12-02T08:00:00Z", "to": "2026-12-02T08:00:00Z"},
        {"from": "2026-12-02T09:00:00Z", "to": "2026-12-02T08:00:00Z"},
    ],
)
async def test_query_errors_are_422_before_sql(
    list_client: tuple[httpx.AsyncClient, AsyncMock], params: dict[str, str]
) -> None:
    client, service = list_client
    response = await client.get("/lessons", params=params)
    assert CONTRACT.assert_error(response, 422, "VALIDATION_ERROR")["fields"]
    service.list_lessons.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    [
        LessonStatus.CONFIRMED,
        LessonStatus.CANCELLED,
        LessonStatus.IN_PROGRESS,
        LessonStatus.COMPLETED,
        LessonStatus.RESCHEDULED,
    ],
)
@pytest.mark.parametrize(
    "patch", [{"ends_at": "2026-12-02T06:00:00Z"}, {"starts_at": "not-a-date"}]
)
async def test_noneditable_precedes_bad_interval_and_upstream(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    status: LessonStatus,
    patch: dict[str, str],
) -> None:
    client, factory, people = command_client
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=row(status)))
    response = await client.patch(f"/lessons/{L1}", json=patch)
    assert CONTRACT.assert_error(response, 409, "LESSON_NOT_EDITABLE") == {
        "current_status": status.value
    }
    people.get_lesson_people.assert_not_awaited()
    assert factory.call_count == 1


@pytest.mark.parametrize("lesson_id", [str(L1), "bad-id"])
async def test_patch_path_error_precedes_value_validation(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    lesson_id: str,
) -> None:
    client, factory, people = command_client
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(scheduling, "get_lesson", lookup)
    response = await client.patch(f"/lessons/{lesson_id}", json={"starts_at": "bad-date"})
    CONTRACT.assert_error(
        response,
        404 if lesson_id == str(L1) else 400,
        "NOT_FOUND" if lesson_id == str(L1) else "BAD_REQUEST",
    )
    people.get_lesson_people.assert_not_awaited()
    assert factory.call_count == (1 if lesson_id == str(L1) else 0)


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"ends_at": "2026-12-02T06:00:00Z"}, "VALIDATION_ERROR"),
        ({"classroom_id": None}, "CLASSROOM_REQUIRED"),
        ({"format": "ONLINE"}, "VALIDATION_ERROR"),
        ({"starts_at": "2026-12-02T07:00:00"}, "VALIDATION_ERROR"),
    ],
)
async def test_patch_early_rules_before_upstream(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    patch: dict[str, Any],
    code: str,
) -> None:
    client, factory, people = command_client
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=row()))
    response = await client.patch(f"/lessons/{L1}", json=patch)
    CONTRACT.assert_error(response, 422, code)
    people.get_lesson_people.assert_not_awaited()
    assert factory.call_count == 1


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"status": "CONFIRMED"},
        {"group_id": str(G1)},
        {"ends_at": None},
        {"instructor_id": None},
    ],
)
async def test_patch_rejects_empty_forbidden_and_nonnullable_fields(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock], patch: dict[str, Any]
) -> None:
    client, factory, _ = command_client
    response = await client.patch(f"/lessons/{L1}", json=patch)
    CONTRACT.assert_error(response, 422, "VALIDATION_ERROR")
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "INSTRUCTOR_NOT_FOUND"),
        (422, "CLASSROOM_NOT_FOUND"),
        (502, "UPSTREAM_UNAVAILABLE"),
        (504, "UPSTREAM_TIMEOUT"),
    ],
)
async def test_patch_preserves_upstream_errors(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    client, factory, people = command_client
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=row()))
    people.get_lesson_people.side_effect = ApiError(status, code, "upstream failed")
    response = await client.patch(f"/lessons/{L1}", json={"ends_at": "2026-12-02T08:00:00Z"})
    CONTRACT.assert_error(response, status, code)
    assert factory.call_count == 1
