"""Снимки событий asyncapi.yaml: без HTTP, ORM и транспорта брокера."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from app.clock import to_iso_z
from app.domain.lesson import LessonStatus
from app.domain.scheduling import DraftLesson

EventType = Literal["LessonConfirmed", "LessonCancelled", "LessonRescheduled"]
_KEYS: dict[EventType, str] = {
    "LessonConfirmed": "lesson-confirmed",
    "LessonCancelled": "lesson-cancelled",
    "LessonRescheduled": "lesson-rescheduled",
}


@dataclass(frozen=True, slots=True)
class LessonEvent:
    event_id: UUID
    event_type: EventType
    occurred_at: datetime
    lesson_id: UUID
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "occurred_at": to_iso_z(self.occurred_at),
            "idempotency_key": f"{_KEYS[self.event_type]}:{self.lesson_id}",
            "producer": "timetable-service",
            "version": 1,
            "payload": self.payload,
        }


def interval(draft: DraftLesson) -> dict[str, Any]:
    return {
        "starts_at": to_iso_z(draft.starts_at),
        "ends_at": to_iso_z(draft.ends_at),
        "instructor_id": str(draft.instructor_id),
        "classroom_id": str(draft.classroom_id) if draft.classroom_id else None,
    }


def confirmed(lesson_id: UUID, draft: DraftLesson, now: datetime) -> LessonEvent:
    return LessonEvent(
        uuid4(),
        "LessonConfirmed",
        now,
        lesson_id,
        {
            "lesson_id": str(lesson_id),
            "group_id": str(draft.group_id),
            **interval(draft),
            "format": draft.format.value,
        },
    )


def cancelled(
    lesson_id: UUID,
    draft: DraftLesson,
    previous_status: LessonStatus,
    reason: str | None,
    now: datetime,
) -> LessonEvent:
    return LessonEvent(
        uuid4(),
        "LessonCancelled",
        now,
        lesson_id,
        {
            "lesson_id": str(lesson_id),
            "group_id": str(draft.group_id),
            "instructor_id": str(draft.instructor_id),
            "starts_at": to_iso_z(draft.starts_at),
            "ends_at": to_iso_z(draft.ends_at),
            "previous_status": previous_status.value,
            "reason": reason,
        },
    )


def rescheduled(
    lesson_id: UUID,
    new_id: UUID,
    previous: DraftLesson,
    new: DraftLesson,
    now: datetime,
) -> LessonEvent:
    return LessonEvent(
        uuid4(),
        "LessonRescheduled",
        now,
        lesson_id,
        {
            "lesson_id": str(lesson_id),
            "rescheduled_to_id": str(new_id),
            "group_id": str(previous.group_id),
            "instructor_id": str(previous.instructor_id),
            "previous": interval(previous),
            "new": interval(new),
        },
    )
