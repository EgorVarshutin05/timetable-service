"""Статусная машина, BR-08 и приоритет проверок API без БД и сети."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import main
from app.api.schemas import RescheduleRequest
from app.clock import AdjustableClock
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.lifecycle import ensure_complete, ensure_transition, is_due
from app.domain.scheduling import InvalidLesson
from app.repositories import scheduling
from app.services.lessons import LessonService, PeopleSource
from app.settings import Settings
from tests.unit.scheduling_data import DRAFT, L1
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_commands_api import command_client as command_client
from tests.unit.test_lesson_listing_and_patch import row

S = LessonStatus
ALLOWED = {
    (S.PLANNED, S.CONFIRMED),
    (S.PLANNED, S.CANCELLED),
    (S.CONFIRMED, S.CANCELLED),
    (S.PLANNED, S.RESCHEDULED),
    (S.CONFIRMED, S.RESCHEDULED),
    (S.CONFIRMED, S.IN_PROGRESS),
    (S.IN_PROGRESS, S.COMPLETED),
}
TARGETS = {
    "confirm": S.CONFIRMED,
    "cancel": S.CANCELLED,
    "complete": S.COMPLETED,
    "reschedule": S.RESCHEDULED,
}
MOVE = {"starts_at": "2026-12-02T07:00:00Z", "ends_at": "2026-12-02T08:30:00Z"}


@pytest.mark.parametrize("current", list(S))
@pytest.mark.parametrize("target", list(S))
def test_transition_matrix(current: S, target: S) -> None:
    if (current, target) in ALLOWED:
        ensure_transition(current, target)
    else:
        with pytest.raises(InvalidLesson) as error:
            ensure_transition(current, target)
        assert error.value.issue.code == "TRANSITION_FORBIDDEN"
        assert error.value.issue.details == {
            "current_status": current.value,
            "target_status": target.value,
        }


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_complete_boundary(delta: int) -> None:
    now = DRAFT.ends_at + timedelta(microseconds=delta)
    if delta < 0:
        with pytest.raises(InvalidLesson) as error:
            ensure_complete(S.IN_PROGRESS, DRAFT.ends_at, now)
        assert error.value.issue.code == "LESSON_NOT_FINISHED"
        assert set(error.value.issue.details) == {"ends_at", "now"}
    else:
        ensure_complete(S.IN_PROGRESS, DRAFT.ends_at, now)


@pytest.mark.parametrize("status", [s for s in S if s != S.IN_PROGRESS])
def test_status_precedes_br08(status: S) -> None:
    with pytest.raises(InvalidLesson) as error:
        ensure_complete(status, DRAFT.ends_at, DRAFT.starts_at)
    assert error.value.issue.code == "TRANSITION_FORBIDDEN"


@pytest.mark.parametrize("status", list(S))
@pytest.mark.parametrize("delta", [-1, 0, 1, 86400])
def test_sweeper_rule(status: S, delta: int) -> None:
    assert is_due(status, DRAFT.starts_at, DRAFT.starts_at + timedelta(seconds=delta)) == (
        status == S.CONFIRMED and delta >= 0
    )


def test_reschedule_inherits_and_distinguishes_explicit_null() -> None:
    inherited = RescheduleRequest.model_validate(MOVE).apply(DRAFT)
    assert inherited == DRAFT
    online = RescheduleRequest.model_validate(MOVE | {"format": "ONLINE", "classroom_id": None})
    assert online.model_fields_set == {"starts_at", "ends_at", "format", "classroom_id"}
    assert online.apply(DRAFT).classroom_id is None
    assert online.apply(DRAFT).format == LessonFormat.ONLINE


@pytest.mark.parametrize(
    ("operation", "status"),
    [(op, status) for op in TARGETS for status in S if (status, TARGETS[op]) not in ALLOWED],
)
async def test_forbidden_api_transitions(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status: S,
) -> None:
    client, _, people = command_client
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=row(status)))
    kwargs: dict[str, Any] = {}
    if operation == "reschedule":
        kwargs["json"] = {
            "starts_at": "bad-date",
            "ends_at": "also-bad",
            "format": "UNKNOWN",
            "instructor_id": "bad-uuid",
        }
    response = await client.post(f"/lessons/{L1}/{operation}", **kwargs)
    assert CONTRACT.assert_error(response, 409, "TRANSITION_FORBIDDEN") == {
        "current_status": status.value,
        "target_status": TARGETS[operation].value,
    }
    people.get_lesson_people.assert_not_awaited()


@pytest.mark.parametrize("operation", list(TARGETS))
@pytest.mark.parametrize("bad_path", [True, False])
async def test_lifecycle_path_errors(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    bad_path: bool,
) -> None:
    client, _, people = command_client
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(scheduling, "get_lesson", lookup)
    response = await client.post(
        f"/lessons/{'bad' if bad_path else L1}/{operation}",
        json=MOVE if operation == "reschedule" else None,
    )
    # confirm/complete не перечисляют 400 в операции, но общий §3.3 требует BAD_REQUEST.
    assert response.status_code == (400 if bad_path else 404)
    assert response.json()["error"]["code"] == ("BAD_REQUEST" if bad_path else "NOT_FOUND")
    people.get_lesson_people.assert_not_awaited()
    if bad_path:
        lookup.assert_not_awaited()


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"ends_at": "2026-12-02T06:00:00Z"}, "VALIDATION_ERROR"),
        ({"starts_at": "2026-12-02T07:00:00"}, "VALIDATION_ERROR"),
        ({"classroom_id": None}, "CLASSROOM_REQUIRED"),
        ({"format": "ONLINE"}, "VALIDATION_ERROR"),
    ],
)
async def test_reschedule_early_rules_before_people(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    code: str,
) -> None:
    client, factory, people = command_client
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=row()))
    response = await client.post(f"/lessons/{L1}/reschedule", json=MOVE | changes)
    CONTRACT.assert_error(response, 422, code)
    people.get_lesson_people.assert_not_awaited()
    assert factory.call_count == 1


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"reason": 123}, 400),
        ({"reason": "x" * 501}, 422),
        ({"unknown": True}, 422),
    ],
)
async def test_cancel_invalid_body(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    body: dict[str, Any],
    status: int,
) -> None:
    client, factory, _ = command_client
    response = await client.post(f"/lessons/{L1}/cancel", json=body)
    CONTRACT.assert_error(response, status, "BAD_REQUEST" if status == 400 else "VALIDATION_ERROR")
    factory.assert_not_called()


async def test_complete_api_reports_br08_without_calling_people(
    command_client: tuple[httpx.AsyncClient, MagicMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, people = command_client
    item = row(S.IN_PROGRESS)
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=item))
    monkeypatch.setattr(AdjustableClock, "now", lambda _: DRAFT.starts_at)
    response = await client.post(f"/lessons/{L1}/complete")
    assert CONTRACT.assert_error(response, 409, "LESSON_NOT_FINISHED") == {
        "ends_at": "2026-12-02T08:30:00Z",
        "now": "2026-12-02T07:00:00Z",
    }
    assert item.status == S.IN_PROGRESS
    people.get_lesson_people.assert_not_awaited()


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_confirm_returns_only_after_successful_commit(
    monkeypatch: pytest.MonkeyPatch, commit_fails: bool
) -> None:
    session = AsyncMock(spec=AsyncSession)
    factory = MagicMock(spec=async_sessionmaker)
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=row()))
    clock = AdjustableClock()
    clock.freeze(datetime(2026, 11, 30, 3, tzinfo=UTC))
    service = LessonService(
        cast(async_sessionmaker[AsyncSession], factory), cast(PeopleSource, AsyncMock()), clock
    )
    if commit_fails:
        session.commit.side_effect = RuntimeError("commit failed")
        with pytest.raises(RuntimeError, match="commit failed"):
            await service.confirm(L1)
        session.rollback.assert_awaited_once()
    else:
        result = await service.confirm(L1)
        session.commit.assert_awaited_once()
        assert result.status == S.CONFIRMED
        assert result.updated_at == clock.now()


@pytest.mark.parametrize("test_mode", [True, False])
async def test_lifespan_registers_and_schedules_sweeper(
    monkeypatch: pytest.MonkeyPatch, test_mode: bool
) -> None:
    settings = Settings(test_mode=test_mode)
    context = main.build_context(settings)
    assert context.jobs.names() == ["lesson-status-sweeper"]
    swept = asyncio.Event()

    async def sweep(factory: async_sessionmaker[AsyncSession], clock: AdjustableClock) -> int:
        assert factory is context.session_factory
        assert clock is context.clock
        swept.set()
        return 7

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_context", lambda _: context)
    monkeypatch.setattr(main, "sweep_lessons", sweep)
    app = main.create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        if test_mode:
            assert not swept.is_set()
            result = await client.post("/__test/jobs/lesson-status-sweeper/run")
            assert result.json() == {"processed": 7}
            assert swept.is_set()
            unknown = await client.post("/__test/jobs/unknown/run")
            assert unknown.status_code == 404
        else:
            async with asyncio.timeout(2):
                await swept.wait()
            hidden = await client.post("/__test/jobs/lesson-status-sweeper/run")
            assert hidden.status_code == 404
