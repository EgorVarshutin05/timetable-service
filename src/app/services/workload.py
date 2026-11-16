"""Профиль до SQL; семантика отсутствующего ресурса пути относится к HTTP-контексту."""

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.people import InstructorProfile
from app.domain.scheduling import InvalidLesson
from app.domain.workload import WeeklyWorkload, parse_week, summarize_workload, week_bounds
from app.errors import ApiError, not_found
from app.repositories.workload import get_week_lessons


class InstructorSource(Protocol):
    async def get_instructor(self, instructor_id: UUID) -> InstructorProfile: ...


class WorkloadService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], people: InstructorSource) -> None:
        self.factory, self.people = factory, people

    async def get(self, instructor_id: UUID, week: str) -> WeeklyWorkload:
        try:
            period = parse_week(week)
        except InvalidLesson as exc:
            issue = exc.issue
            raise ApiError(422, issue.code, issue.message, issue.details) from exc
        try:
            profile = await self.people.get_instructor(instructor_id)
        except ApiError as exc:
            if exc.status_code == 422 and exc.code == "INSTRUCTOR_NOT_FOUND":
                raise not_found(
                    "Преподаватель не найден", instructor_id=str(instructor_id)
                ) from exc
            raise
        try:
            start, end = week_bounds(period, profile.timezone)
        except InvalidLesson as exc:
            issue = exc.issue
            raise ApiError(422, issue.code, issue.message, issue.details) from exc
        async with self.factory() as session:
            lessons = await get_week_lessons(session, instructor_id, start, end)
        return summarize_workload(profile, period, lessons)
