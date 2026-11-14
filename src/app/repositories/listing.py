"""FR-03: одинаковый фильтр для total и страницы; порядок starts_at, id."""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.listing import LessonFilters
from app.models import Lesson


def filtered_query(filters: LessonFilters) -> Select[tuple[Lesson]]:
    statement = select(Lesson)
    for column, value in (
        (Lesson.group_id, filters.group_id),
        (Lesson.instructor_id, filters.instructor_id),
        (Lesson.classroom_id, filters.classroom_id),
        (Lesson.status, filters.status),
    ):
        if value is not None:
            statement = statement.where(column == value)
    if filters.starts_from is not None:
        statement = statement.where(Lesson.starts_at >= filters.starts_from)
    if filters.starts_to is not None:
        statement = statement.where(Lesson.starts_at < filters.starts_to)
    return statement


async def get_page(session: AsyncSession, filters: LessonFilters) -> tuple[list[Lesson], int]:
    statement = filtered_query(filters)
    count = await session.scalar(select(func.count()).select_from(statement.subquery()))
    items = (
        await session.scalars(
            statement.order_by(Lesson.starts_at, Lesson.id)
            .limit(filters.limit)
            .offset(filters.offset)
        )
    ).all()
    return list(items), int(count or 0)
