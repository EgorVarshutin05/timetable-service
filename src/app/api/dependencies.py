"""Зависимости HTTP-слоя; GET не фиксирует транзакции с изменениями."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext, get_context
from app.errors import ApiError

Context = Annotated[AppContext, Depends(get_context)]


async def get_session(context: Context) -> AsyncIterator[AsyncSession]:
    async with context.session_factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def parse_resource_id(value: str, field: str) -> UUID:
    """Неверный UUID — ошибка формата 400, до обращения к БД."""
    try:
        return UUID(value)
    except ValueError:
        raise ApiError(
            400,
            "BAD_REQUEST",
            "Невалидный UUID",
            {"errors": [{"field": field, "type": "uuid_parsing"}]},
        ) from None


def group_id_from_path(group_id: str) -> UUID:
    return parse_resource_id(group_id, "group_id")


def lesson_id_from_path(lesson_id: str) -> UUID:
    return parse_resource_id(lesson_id, "lesson_id")


GroupId = Annotated[UUID, Depends(group_id_from_path)]
LessonId = Annotated[UUID, Depends(lesson_id_from_path)]
