"""Стандартные структуры для изолированных тестов предметных правил."""

from datetime import UTC, datetime, time
from decimal import Decimal
from uuid import UUID

from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.people import (
    AvailabilityWindow,
    Classroom,
    InstructorAvailability,
    InstructorProfile,
    LessonPeople,
)
from app.domain.scheduling import DraftLesson, ScheduledLesson

G1 = UUID("9a000000-0000-4000-8000-000000000001")
G2 = UUID("9a000000-0000-4000-8000-000000000002")
I1 = UUID("1b000000-0000-4000-8000-000000000001")
I2 = UUID("1b000000-0000-4000-8000-000000000002")
C1 = UUID("2c000000-0000-4000-8000-000000000001")
C2 = UUID("2c000000-0000-4000-8000-000000000002")
L1 = UUID("3d000000-0000-4000-8000-000000000001")
DRAFT = DraftLesson(
    G1,
    I1,
    C1,
    LessonFormat.IN_PERSON,
    datetime(2026, 12, 2, 7, tzinfo=UTC),
    datetime(2026, 12, 2, 8, 30, tzinfo=UTC),
)
PEOPLE = LessonPeople(
    InstructorProfile(I1, "Преподаватель", Decimal(6), "Asia/Yekaterinburg"),
    InstructorAvailability(
        I1,
        "Asia/Yekaterinburg",
        tuple(AvailabilityWindow(day, time(9), time(18)) for day in range(1, 8)),
    ),
    Classroom(C1, "Аудитория", 30, True),
)


def existing(draft: DraftLesson = DRAFT, lesson_id: UUID = L1) -> ScheduledLesson:
    return ScheduledLesson(
        lesson_id,
        draft.group_id,
        draft.instructor_id,
        draft.classroom_id,
        draft.starts_at,
        draft.ends_at,
        LessonStatus.PLANNED,
    )
