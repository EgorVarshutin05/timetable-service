"""Зависимости HTTP-слоя; GET не фиксирует транзакции с изменениями."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import LessonQuery
from app.context import AppContext, get_context
from app.domain.listing import LessonFilters, validate_filters
from app.domain.scheduling import InvalidLesson
from app.errors import ApiError
from app.services.lessons import LessonService

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


def get_lesson_service(context: Context) -> LessonService:
    if context.people_client is None:
        raise RuntimeError("people_client не подключён")
    return LessonService(context.session_factory, context.people_client, context.clock)


Lessons = Annotated[LessonService, Depends(get_lesson_service)]


def get_lesson_filters(request: Request) -> LessonFilters:
    try:
        query = LessonQuery.model_validate(dict(request.query_params))
        return validate_filters(query.to_filters())
    except ValidationError as exc:
        fields = {
            ".".join(str(part) for part in issue["loc"]): issue["msg"] for issue in exc.errors()
        }
        raise ApiError(
            422, "VALIDATION_ERROR", "Невалидные параметры списка", {"fields": fields}
        ) from exc
    except InvalidLesson as exc:
        issue = exc.issue
        raise ApiError(422, issue.code, issue.message, issue.details) from exc


Filters = Annotated[LessonFilters, Depends(get_lesson_filters)]
