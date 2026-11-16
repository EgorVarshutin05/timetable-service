"""Отсутствующий ресурс из пути превращается в платформенный NOT_FOUND."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import not_found
from app.models import Group, Lesson
from app.repositories import catalog


async def list_groups(session: AsyncSession, limit: int, offset: int) -> tuple[list[Group], int]:
    # Два SELECT видят один снимок даже при конкурентных INSERT/DELETE.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    return await catalog.get_groups_page(session, limit, offset)


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
