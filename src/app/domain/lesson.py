"""Значения формата и статуса из нормативного контракта."""

from enum import StrEnum


class LessonFormat(StrEnum):
    IN_PERSON = "IN_PERSON"
    ONLINE = "ONLINE"


class LessonStatus(StrEnum):
    PLANNED = "PLANNED"
    CONFIRMED = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
