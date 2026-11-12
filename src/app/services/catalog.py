"""Отсутствующий ресурс из пути превращается в платформенный NOT_FOUND."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import not_found
from app.models import Group, Lesson
from app.repositories import catalog


async def get_group(session: AsyncSession, group_id: UUID) -> Group:
    group = await catalog.get_group(session, group_id)
    if group is None:
        raise not_found("Группа не найдена", group_id=str(group_id))
    return group


async def get_lesson(session: AsyncSession, lesson_id: UUID) -> Lesson:
    lesson = await catalog.get_lesson(session, lesson_id)
    if lesson is None:
        raise not_found("Занятие не найдено", lesson_id=str(lesson_id))
    return lesson
