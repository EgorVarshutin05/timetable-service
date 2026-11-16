"""Чтение ресурсов по первичному ключу."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, Lesson


async def get_groups_page(
    session: AsyncSession, limit: int, offset: int
) -> tuple[list[Group], int]:
    total = await session.scalar(select(func.count()).select_from(Group))
    items = await session.scalars(
        select(Group).order_by(Group.name, Group.id).limit(limit).offset(offset)
    )
    return list(items.all()), int(total or 0)


async def get_group(session: AsyncSession, group_id: UUID) -> Group | None:
    return await session.get(Group, group_id)


async def get_lesson(session: AsyncSession, lesson_id: UUID) -> Lesson | None:
    return await session.get(Lesson, lesson_id)
