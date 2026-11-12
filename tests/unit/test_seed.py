"""Seed проверяется без сети; он не фиксирует чужую транзакцию и сохраняет UUID/даты."""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.seed import load_seed, read_all


async def test_seed_uses_typed_values_and_targeted_on_conflict() -> None:
    session = AsyncMock(spec=AsyncSession)
    await load_seed(cast(AsyncSession, session), read_all())
    assert session.execute.await_count == 2
    for call in session.execute.await_args_list:
        statement = cast(Insert, call.args[0]).compile()
        assert "ON CONFLICT (id) DO NOTHING" in str(statement)
        assert isinstance(statement.params["id_m0"], UUID)
    lessons = cast(Insert, session.execute.await_args_list[1].args[0]).compile()
    assert lessons.params["starts_at_m0"] == datetime(2026, 12, 1, 5, tzinfo=UTC)
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
