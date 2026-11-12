"""Прямые INSERT seed и полная очистка принадлежащих сервису таблиц."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, Lesson


async def insert_missing(
    session: AsyncSession, model: type[Group] | type[Lesson], rows: Sequence[dict[str, Any]]
) -> None:
    if rows:
        statement = insert(model).values(list(rows)).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(statement)


async def clear_data(session: AsyncSession) -> None:
    # Обе таблицы перечислены явно: TRUNCATE транзакционен, CASCADE не нужен.
    await session.execute(text("TRUNCATE TABLE lessons, groups"))
