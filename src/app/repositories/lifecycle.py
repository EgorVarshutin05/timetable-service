"""Выборка наступивших подтверждённых занятий с блокировкой строк."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.lesson import LessonStatus
from app.models import Lesson


async def get_due(session: AsyncSession, now: datetime) -> list[Lesson]:
    statement = (
        select(Lesson)
        .where(Lesson.status == LessonStatus.CONFIRMED, Lesson.starts_at <= now)
        .order_by(Lesson.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list((await session.scalars(statement)).all())
