"""Транзакция фонового перехода CONFIRMED → IN_PROGRESS."""

from datetime import UTC

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.db import session_scope
from app.domain.lesson import LessonStatus
from app.domain.lifecycle import ensure_transition, is_due
from app.repositories.lifecycle import get_due


async def sweep_lessons(factory: async_sessionmaker[AsyncSession], clock: Clock) -> int:
    now = clock.now().astimezone(UTC)
    async with session_scope(factory) as session:
        lessons = await get_due(session, now)
        processed = 0
        for lesson in lessons:
            if is_due(lesson.status, lesson.starts_at, now):
                ensure_transition(lesson.status, LessonStatus.IN_PROGRESS)
                lesson.status = LessonStatus.IN_PROGRESS
                lesson.updated_at = now
                processed += 1
    return processed
