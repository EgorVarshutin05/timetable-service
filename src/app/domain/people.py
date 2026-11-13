"""Данные people-сервиса без HTTP, Pydantic и SQLAlchemy."""

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class InstructorProfile:
    id: UUID
    full_name: str
    max_hours_per_day: Decimal
    timezone: str


@dataclass(frozen=True, slots=True)
class AvailabilityWindow:
    weekday: int
    starts_at: time
    ends_at: time


@dataclass(frozen=True, slots=True)
class InstructorAvailability:
    instructor_id: UUID
    timezone: str
    windows: tuple[AvailabilityWindow, ...]


@dataclass(frozen=True, slots=True)
class Classroom:
    id: UUID
    name: str
    capacity: int
    online_capable: bool


@dataclass(frozen=True, slots=True)
class LessonPeople:
    instructor: InstructorProfile
    availability: InstructorAvailability
    classroom: Classroom | None
