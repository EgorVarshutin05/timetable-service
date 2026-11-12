"""Чтение ресурсов по первичному ключу."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, Lesson


async def get_group(session: AsyncSession, group_id: UUID) -> Group | None:
    return await session.get(Group, group_id)


async def get_lesson(session: AsyncSession, lesson_id: UUID) -> Lesson | None:
    return await session.get(Lesson, lesson_id)
