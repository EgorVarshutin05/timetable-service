"""Активные занятия преподавателя, пересекающие UTC-границы локальной недели."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.scheduling import ACTIVE_STATUSES, ScheduledLesson
from app.models import Lesson


async def get_week_lessons(
    session: AsyncSession, instructor_id: UUID, start: datetime, end: datetime
) -> list[ScheduledLesson]:
    statement = (
        select(Lesson)
        .where(
            Lesson.instructor_id == instructor_id,
            Lesson.status.in_(ACTIVE_STATUSES),
            Lesson.starts_at < end,
            Lesson.ends_at > start,
        )
        .order_by(Lesson.starts_at, Lesson.id)
    )
    rows = (await session.scalars(statement)).all()
    return [
        ScheduledLesson(
            row.id,
            row.group_id,
            row.instructor_id,
            row.classroom_id,
            row.starts_at,
            row.ends_at,
            row.status,
        )
        for row in rows
    ]
