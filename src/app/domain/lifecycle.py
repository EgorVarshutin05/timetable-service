"""Чистая статусная машина и BR-08; время передаётся вызывающим кодом."""

from datetime import datetime

from app.clock import to_iso_z
from app.domain.lesson import LessonStatus
from app.domain.scheduling import InvalidLesson, Issue

_SOURCES = {
    LessonStatus.CONFIRMED: frozenset({LessonStatus.PLANNED}),
    LessonStatus.CANCELLED: frozenset({LessonStatus.PLANNED, LessonStatus.CONFIRMED}),
    LessonStatus.RESCHEDULED: frozenset({LessonStatus.PLANNED, LessonStatus.CONFIRMED}),
    LessonStatus.IN_PROGRESS: frozenset({LessonStatus.CONFIRMED}),
    LessonStatus.COMPLETED: frozenset({LessonStatus.IN_PROGRESS}),
}


def ensure_transition(current: LessonStatus, target: LessonStatus) -> None:
    if current not in _SOURCES.get(target, frozenset()):
        raise InvalidLesson(
            Issue(
                "TRANSITION_FORBIDDEN",
                "Недопустимый переход",
                {"current_status": current.value, "target_status": target.value},
            )
        )


def ensure_complete(current: LessonStatus, ends_at: datetime, now: datetime) -> None:
    ensure_transition(current, LessonStatus.COMPLETED)
    if now < ends_at:
        raise InvalidLesson(
            Issue(
                "LESSON_NOT_FINISHED",
                "Занятие ещё не закончилось",
                {"ends_at": to_iso_z(ends_at), "now": to_iso_z(now)},
            )
        )


def is_due(current: LessonStatus, starts_at: datetime, now: datetime) -> bool:
    return current == LessonStatus.CONFIRMED and starts_at <= now
