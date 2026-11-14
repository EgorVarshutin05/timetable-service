"""Оркестрация расписания и жизненного цикла; успешный ответ только после commit."""

from collections.abc import Callable
from datetime import UTC
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker import EventPublisher
from app.clock import Clock
from app.db import session_scope
from app.domain import events
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.lifecycle import ensure_complete, ensure_transition
from app.domain.listing import LessonFilters, validate_filters
from app.domain.people import LessonPeople
from app.domain.scheduling import (
    Assessment,
    DraftLesson,
    InvalidLesson,
    Issue,
    availability_warning,
    capacity_violation,
    conflict_violation,
    ensure_editable,
    validate_early,
    workload_span,
    workload_warnings,
)
from app.errors import ApiError, not_found
from app.models import Lesson
from app.repositories import listing, scheduling

DraftUpdate = Callable[[DraftLesson], DraftLesson]


class PeopleSource(Protocol):
    async def get_lesson_people(
        self, *, instructor_id: UUID, lesson_format: LessonFormat, classroom_id: UUID | None = None
    ) -> LessonPeople: ...


def _as_error(issue: Issue, status: int) -> ApiError:
    return ApiError(status, issue.code, issue.message, issue.details)


def _group_missing(group_id: UUID) -> ApiError:
    return ApiError(422, "GROUP_NOT_FOUND", "Группа не найдена", {"group_id": str(group_id)})


def _editable(lesson: Lesson | None, lesson_id: UUID) -> Lesson:
    if lesson is None:
        raise not_found("Занятие не найдено", lesson_id=str(lesson_id))
    try:
        ensure_editable(lesson.status)
    except InvalidLesson as exc:
        raise _as_error(exc.issue, 409) from exc
    return lesson


def _validated(draft: DraftLesson) -> DraftLesson:
    try:
        return validate_early(draft)
    except InvalidLesson as exc:
        raise _as_error(exc.issue, 422) from exc


def _transitionable(lesson: Lesson | None, lesson_id: UUID, target: LessonStatus) -> Lesson:
    if lesson is None:
        raise not_found("Занятие не найдено", lesson_id=str(lesson_id))
    try:
        ensure_transition(lesson.status, target)
    except InvalidLesson as exc:
        raise _as_error(exc.issue, 409) from exc
    return lesson


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
        self,
        factory: async_sessionmaker[AsyncSession],
        people: PeopleSource,
        clock: Clock,
        publisher: EventPublisher | None = None,
    ) -> None:
        self.factory, self.people, self.clock = factory, people, clock
        # None — только для прямых изолированных тестов прежних пакетов.
        self.publisher = publisher

    async def _transition(
        self, lesson_id: UUID, target: LessonStatus, reason: str | None = None
    ) -> Lesson:
        event: events.LessonEvent | None = None
        async with session_scope(self.factory) as session:
            lesson = _transitionable(
                await scheduling.get_lesson(session, lesson_id, lock=True), lesson_id, target
            )
            now = self.clock.now().astimezone(UTC)
            if target == LessonStatus.CONFIRMED:
                event = events.confirmed(lesson.id, scheduling.lesson_draft(lesson), now)
            elif target == LessonStatus.CANCELLED:
                event = events.cancelled(
                    lesson.id, scheduling.lesson_draft(lesson), lesson.status, reason, now
                )
            if target == LessonStatus.COMPLETED:
                try:
                    ensure_complete(lesson.status, lesson.ends_at, now)
                except InvalidLesson as exc:
                    raise _as_error(exc.issue, 409) from exc
            lesson.status = target
            lesson.updated_at = now
            if target == LessonStatus.CANCELLED:
                lesson.cancel_reason = reason
            await session.flush()
        if event is not None and self.publisher is not None:
            await self.publisher.publish(event)
        return lesson

    async def confirm(self, lesson_id: UUID) -> Lesson:
        lesson = await self._transition(lesson_id, LessonStatus.CONFIRMED)
        return lesson

    async def cancel(self, lesson_id: UUID, reason: str | None = None) -> Lesson:
        lesson = await self._transition(lesson_id, LessonStatus.CANCELLED, reason)
        return lesson

    async def complete(self, lesson_id: UUID) -> Lesson:
        return await self._transition(lesson_id, LessonStatus.COMPLETED)

    async def reschedule(self, lesson_id: UUID, apply: DraftUpdate) -> tuple[Lesson, Assessment]:
        while True:
            async with self.factory() as session:
                current = _transitionable(
                    await scheduling.get_lesson(session, lesson_id),
                    lesson_id,
                    LessonStatus.RESCHEDULED,
                )
                old = scheduling.lesson_draft(current)
            draft = _validated(apply(old))
            people = await self.people.get_lesson_people(
                instructor_id=draft.instructor_id,
                lesson_format=draft.format,
                classroom_id=draft.classroom_id,
            )
            async with session_scope(self.factory) as session:
                await scheduling.lock_resources(session, old, draft)
                original = _transitionable(
                    await scheduling.get_lesson(session, lesson_id, lock=True),
                    lesson_id,
                    LessonStatus.RESCHEDULED,
                )
                if scheduling.lesson_draft(original) != old:
                    # PATCH мог изменить ресурсы/наследуемые поля во время HTTP.
                    continue
                group = await scheduling.get_group(session, draft.group_id, lock=True)
                if group is None:
                    raise _group_missing(draft.group_id)
                report = await assess(
                    session,
                    draft,
                    group.size,
                    people,
                    exclude_lesson_id=lesson_id,
                    collect_all=False,
                )
                now = self.clock.now().astimezone(UTC)
                original.status = LessonStatus.RESCHEDULED
                original.updated_at = now
                lesson = Lesson(
                    id=uuid4(),
                    group_id=original.group_id,
                    instructor_id=draft.instructor_id,
                    classroom_id=draft.classroom_id,
                    format=draft.format,
                    starts_at=draft.starts_at,
                    ends_at=draft.ends_at,
                    status=LessonStatus.PLANNED,
                    rescheduled_from_id=original.id,
                    cancel_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(lesson)
                event = events.rescheduled(original.id, lesson.id, old, draft, now)
                await session.flush()
            if self.publisher is not None:
                await self.publisher.publish(event)
            return lesson, report

    async def list_lessons(self, filters: LessonFilters) -> tuple[list[Lesson], int]:
        try:
            filters = validate_filters(filters)
        except InvalidLesson as exc:
            raise _as_error(exc.issue, 422) from exc
        async with self.factory() as session:
            # Только чтение: total/items видят один снимок; запись остаётся READ COMMITTED.
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return await listing.get_page(session, filters)

    async def patch(self, lesson_id: UUID, apply: DraftUpdate) -> tuple[Lesson, Assessment]:
        while True:
            async with self.factory() as session:
                current = _editable(await scheduling.get_lesson(session, lesson_id), lesson_id)
                old = scheduling.lesson_draft(current)
            # Синтаксис JSON уже разобран, но значения PATCH и BR проверяются только после статуса.
            draft = _validated(apply(old))
            people = await self.people.get_lesson_people(
                instructor_id=draft.instructor_id,
                lesson_format=draft.format,
                classroom_id=draft.classroom_id,
            )
            async with session_scope(self.factory) as session:
                await scheduling.lock_resources(session, old, draft)
                lesson = _editable(
                    await scheduling.get_lesson(session, lesson_id, lock=True), lesson_id
                )
                if scheduling.lesson_draft(lesson) != old:
                    # Конкурентный PATCH изменил снимок. __aexit__ освободит locks до нового HTTP.
                    continue
                group = await scheduling.get_group(session, draft.group_id, lock=True)
                if group is None:
                    raise _group_missing(draft.group_id)
                report = await assess(
                    session,
                    draft,
                    group.size,
                    people,
                    exclude_lesson_id=lesson_id,
                    collect_all=False,
                )
                lesson.instructor_id = draft.instructor_id
                lesson.classroom_id = draft.classroom_id
                lesson.format = draft.format
                lesson.starts_at = draft.starts_at
                lesson.ends_at = draft.ends_at
                lesson.updated_at = self.clock.now().astimezone(UTC)
                await session.flush()
                # Возврат завершается после commit в session_scope.__aexit__.
                return lesson, report

    async def execute(
        self, draft: DraftLesson, *, dry_run: bool
    ) -> tuple[Lesson | None, Assessment]:
        draft = _validated(draft)
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
