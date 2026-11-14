"""Транзакционные блокировки и снимки кандидатов для BR-05/06."""

import hashlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, and_, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.scheduling import ACTIVE_STATUSES, DraftLesson, ScheduledLesson
from app.models import Group, Lesson


def resource_lock_keys(draft: DraftLesson) -> tuple[int, ...]:
    resources = [("instructor", draft.instructor_id), ("group", draft.group_id)]
    if draft.classroom_id is not None:
        resources.append(("classroom", draft.classroom_id))
    return tuple(
        sorted(
            {
                int.from_bytes(
                    hashlib.blake2b(f"timetable:{kind}:{uid}".encode(), digest_size=8).digest(),
                    "big",
                    signed=True,
                )
                for kind, uid in resources
            }
        )
    )


async def lock_resources(
    session: AsyncSession, draft: DraftLesson, *additional: DraftLesson
) -> None:
    statement = text("SELECT pg_advisory_xact_lock(:key)").bindparams(
        bindparam("key", type_=BigInteger)
    )
    keys = sorted({key for item in (draft, *additional) for key in resource_lock_keys(item)})
    for key in keys:
        await session.execute(statement, {"key": key})


async def get_lesson(
    session: AsyncSession, lesson_id: UUID, *, lock: bool = False
) -> Lesson | None:
    statement = select(Lesson).where(Lesson.id == lesson_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await session.scalars(statement)).one_or_none()


def lesson_draft(lesson: Lesson) -> DraftLesson:
    return DraftLesson(
        lesson.group_id,
        lesson.instructor_id,
        lesson.classroom_id,
        lesson.format,
        lesson.starts_at,
        lesson.ends_at,
    )


async def get_group(session: AsyncSession, group_id: UUID, *, lock: bool = False) -> Group | None:
    statement = select(Group).where(Group.id == group_id)
    if lock:
        statement = statement.with_for_update(read=True)
    return (await session.scalars(statement)).one_or_none()


async def get_candidates(
    session: AsyncSession,
    draft: DraftLesson,
    day_start: datetime,
    day_end: datetime,
    exclude_lesson_id: UUID | None = None,
) -> list[ScheduledLesson]:
    resources = [Lesson.group_id == draft.group_id, Lesson.instructor_id == draft.instructor_id]
    if draft.classroom_id is not None:
        resources.append(Lesson.classroom_id == draft.classroom_id)
    statement = select(Lesson).where(
        Lesson.status.in_(ACTIVE_STATUSES),
        or_(
            and_(
                Lesson.starts_at < draft.ends_at, Lesson.ends_at > draft.starts_at, or_(*resources)
            ),
            and_(
                Lesson.instructor_id == draft.instructor_id,
                Lesson.starts_at < day_end,
                Lesson.ends_at > day_start,
            ),
        ),
    )
    if exclude_lesson_id is not None:
        statement = statement.where(Lesson.id != exclude_lesson_id)
    rows = (await session.scalars(statement.order_by(Lesson.id))).all()
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
