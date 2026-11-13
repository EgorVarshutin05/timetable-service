"""FR-01/02: порядок проверок, транзакция и создание только после успешных правил."""

from datetime import UTC
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.db import session_scope
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.people import LessonPeople
from app.domain.scheduling import (
    Assessment,
    DraftLesson,
    InvalidLesson,
    Issue,
    availability_warning,
    capacity_violation,
    conflict_violation,
    validate_early,
    workload_span,
    workload_warnings,
)
from app.errors import ApiError
from app.models import Lesson
from app.repositories import scheduling


class PeopleSource(Protocol):
    async def get_lesson_people(
        self, *, instructor_id: UUID, lesson_format: LessonFormat, classroom_id: UUID | None = None
    ) -> LessonPeople: ...


def _as_error(issue: Issue, status: int) -> ApiError:
    return ApiError(status, issue.code, issue.message, issue.details)


def _group_missing(group_id: UUID) -> ApiError:
    return ApiError(422, "GROUP_NOT_FOUND", "Группа не найдена", {"group_id": str(group_id)})


async def assess(
    session: AsyncSession,
    draft: DraftLesson,
    group_size: int,
    people: LessonPeople,
    exclude_lesson_id: UUID | None = None,
    *,
    collect_all: bool = True,
) -> Assessment:
    """Сессия уже держит resource locks; переиспользуется будущими PATCH/reschedule."""
    violations: list[Issue] = []
    capacity = capacity_violation(draft, group_size, people)
    if capacity is not None:
        if not collect_all:
            raise _as_error(capacity, 409)
        violations.append(capacity)
    start, end = workload_span(draft, people.instructor.timezone)
    existing = await scheduling.get_candidates(session, draft, start, end, exclude_lesson_id)
    conflict = conflict_violation(draft, existing, exclude_lesson_id)
    if conflict is not None:
        if not collect_all:
            raise _as_error(conflict, 409)
        violations.append(conflict)
    warnings = list(workload_warnings(draft, existing, people, exclude_lesson_id))
    unavailable = availability_warning(draft, people)
    if unavailable is not None:
        warnings.append(unavailable)
    return Assessment(tuple(violations), tuple(warnings))


class LessonService:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], people: PeopleSource, clock: Clock
    ) -> None:
        self.factory, self.people, self.clock = factory, people, clock

    async def execute(
        self, draft: DraftLesson, *, dry_run: bool
    ) -> tuple[Lesson | None, Assessment]:
        try:
            draft = validate_early(draft)
        except InvalidLesson as exc:
            raise _as_error(exc.issue, 422) from exc
        # Короткая сессия: группа проверяется до HTTP; соединение на время HTTP освобождено.
        async with self.factory() as session:
            if await scheduling.get_group(session, draft.group_id) is None:
                raise _group_missing(draft.group_id)
        people = await self.people.get_lesson_people(
            instructor_id=draft.instructor_id,
            lesson_format=draft.format,
            classroom_id=draft.classroom_id,
        )
        lesson: Lesson | None = None
        async with session_scope(self.factory) as session:
            await scheduling.lock_resources(session, draft)
            group = await scheduling.get_group(session, draft.group_id, lock=True)
            if group is None:
                raise _group_missing(draft.group_id)
            report = await assess(session, draft, group.size, people, collect_all=dry_run)
            if not dry_run:
                now = self.clock.now().astimezone(UTC)
                lesson = Lesson(
                    id=uuid4(),
                    group_id=draft.group_id,
                    instructor_id=draft.instructor_id,
                    classroom_id=draft.classroom_id,
                    format=draft.format,
                    starts_at=draft.starts_at,
                    ends_at=draft.ends_at,
                    status=LessonStatus.PLANNED,
                    rescheduled_from_id=None,
                    cancel_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(lesson)
                await session.flush()
        # Commit выполнен session_scope до возврата; validate не создаёт строки и события.
        return lesson, report
