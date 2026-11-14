"""BR-01/02/04..07: чистые функции над снимками данных, без HTTP и БД."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.clock import to_iso_z
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.people import AvailabilityWindow, LessonPeople

ACTIVE_STATUSES = frozenset(
    {LessonStatus.PLANNED, LessonStatus.CONFIRMED, LessonStatus.IN_PROGRESS, LessonStatus.COMPLETED}
)


@dataclass(frozen=True, slots=True)
class DraftLesson:
    group_id: UUID
    instructor_id: UUID
    classroom_id: UUID | None
    format: LessonFormat
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledLesson:
    id: UUID
    group_id: UUID
    instructor_id: UUID
    classroom_id: UUID | None
    starts_at: datetime
    ends_at: datetime
    status: LessonStatus


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Assessment:
    violations: tuple[Issue, ...]
    warnings: tuple[Issue, ...]


class InvalidLesson(Exception):
    def __init__(self, issue: Issue) -> None:
        super().__init__(issue.message)
        self.issue = issue


def ensure_editable(status: LessonStatus) -> None:
    if status != LessonStatus.PLANNED:
        raise InvalidLesson(
            Issue(
                "LESSON_NOT_EDITABLE", "Занятие нельзя изменить", {"current_status": status.value}
            )
        )


def _invalid(field: str, message: str) -> InvalidLesson:
    return InvalidLesson(
        Issue("VALIDATION_ERROR", "Невалидные данные", {"fields": {field: message}})
    )


def validate_early(draft: DraftLesson) -> DraftLesson:
    """Aware → UTC, BR-01 → BR-02. Вызывать до БД и people-stub."""
    for field, moment in (("starts_at", draft.starts_at), ("ends_at", draft.ends_at)):
        if moment.utcoffset() is None:
            raise _invalid(field, "Время должно содержать смещение часового пояса")
    draft = replace(
        draft, starts_at=draft.starts_at.astimezone(UTC), ends_at=draft.ends_at.astimezone(UTC)
    )
    duration = draft.ends_at - draft.starts_at
    if duration <= timedelta(0):
        raise _invalid("ends_at", "Конец должен быть позже начала")
    if not timedelta(minutes=30) <= duration <= timedelta(hours=8):
        raise _invalid("ends_at", "Длительность должна быть от 30 минут до 8 часов")
    if draft.format == LessonFormat.IN_PERSON and draft.classroom_id is None:
        raise InvalidLesson(Issue("CLASSROOM_REQUIRED", "Для очного занятия нужна аудитория", {}))
    if draft.format == LessonFormat.ONLINE and draft.classroom_id is not None:
        raise _invalid("classroom_id", "Для ONLINE аудитория должна быть null")
    return draft


def capacity_violation(draft: DraftLesson, group_size: int, people: LessonPeople) -> Issue | None:
    if draft.format == LessonFormat.IN_PERSON:
        if people.classroom is None:
            raise ValueError("Нет данных аудитории после BR-03")
        if people.classroom.capacity < group_size:
            return Issue(
                "CLASSROOM_TOO_SMALL",
                "Аудитория не вмещает группу",
                {"capacity": people.classroom.capacity, "group_size": group_size},
            )
    return None


def conflict_violation(
    draft: DraftLesson,
    lessons: Iterable[ScheduledLesson],
    exclude_lesson_id: UUID | None = None,
) -> Issue | None:
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for lesson in lessons:
        if lesson.id == exclude_lesson_id or lesson.status not in ACTIVE_STATUSES:
            continue
        start, end = max(draft.starts_at, lesson.starts_at), min(draft.ends_at, lesson.ends_at)
        if start >= end:
            continue
        resources = (
            ("instructor", draft.instructor_id == lesson.instructor_id),
            (
                "classroom",
                draft.classroom_id is not None and draft.classroom_id == lesson.classroom_id,
            ),
            ("group", draft.group_id == lesson.group_id),
        )
        for rank, (resource, same) in enumerate(resources):
            if same:
                matches.append(
                    (
                        rank,
                        str(lesson.id),
                        {
                            "resource": resource,
                            "lesson_id": str(lesson.id),
                            "overlap": {"starts_at": to_iso_z(start), "ends_at": to_iso_z(end)},
                        },
                    )
                )
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]))
    return Issue(
        "SCHEDULE_CONFLICT",
        "Занятие пересекается с расписанием",
        {"conflicts": [item[2] for item in matches]},
    )


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def daily_hours(starts_at: datetime, ends_at: datetime, timezone: str) -> dict[date, Decimal]:
    """Разрезать UTC-интервал по локальным датам; считать физическую длительность при DST."""
    zone = ZoneInfo(timezone)
    start, end = starts_at.astimezone(UTC), ends_at.astimezone(UTC)
    day, last = start.astimezone(zone).date(), end.astimezone(zone).date()
    result: dict[date, Decimal] = {}
    while day <= last:
        lo, hi = (
            max(start, _midnight(day, zone)),
            min(end, _midnight(day + timedelta(days=1), zone)),
        )
        if lo < hi:
            elapsed = hi - lo
            microseconds = (
                elapsed.days * 86400 + elapsed.seconds
            ) * 1_000_000 + elapsed.microseconds
            result[day] = Decimal(microseconds) / Decimal(3_600_000_000)
        day += timedelta(days=1)
    return result


def workload_span(draft: DraftLesson, timezone: str) -> tuple[datetime, datetime]:
    days = daily_hours(draft.starts_at, draft.ends_at, timezone)
    zone = ZoneInfo(timezone)
    return _midnight(min(days), zone), _midnight(max(days) + timedelta(days=1), zone)


def workload_warnings(
    draft: DraftLesson,
    lessons: Iterable[ScheduledLesson],
    people: LessonPeople,
    exclude_lesson_id: UUID | None = None,
) -> tuple[Issue, ...]:
    profile = people.instructor
    planned = daily_hours(draft.starts_at, draft.ends_at, profile.timezone)
    for lesson in lessons:
        if (
            lesson.id == exclude_lesson_id
            or lesson.status not in ACTIVE_STATUSES
            or lesson.instructor_id != draft.instructor_id
        ):
            continue
        for day, hours in daily_hours(lesson.starts_at, lesson.ends_at, profile.timezone).items():
            if day in planned:
                planned[day] += hours
    return tuple(
        Issue(
            "WORKLOAD_SOFT_LIMIT",
            "Превышена дневная норма преподавателя",
            {
                "date": day.isoformat(),
                "max_hours": float(profile.max_hours_per_day),
                "planned_hours": float(hours),
            },
        )
        for day, hours in sorted(planned.items())
        if hours > profile.max_hours_per_day
    )


def _window_bounds(
    day: date, window: AvailabilityWindow, zone: ZoneInfo
) -> tuple[datetime, datetime] | None:
    start = datetime.combine(day, window.starts_at, tzinfo=zone)
    end = datetime.combine(day, window.ends_at, tzinfo=zone)
    # Неоднозначная граница: первое вхождение (fold=0); несуществующая граница не образует окно.
    for boundary in (start, end):
        if boundary.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != boundary.replace(
            tzinfo=None
        ):
            return None
    return start.astimezone(UTC), end.astimezone(UTC)


def availability_warning(draft: DraftLesson, people: LessonPeople) -> Issue | None:
    zone = ZoneInfo(people.instructor.timezone)
    day = draft.starts_at.astimezone(zone).date()
    for window in people.availability.windows:
        if window.weekday == day.isoweekday():
            bounds = _window_bounds(day, window, zone)
            if bounds is not None and bounds[0] <= draft.starts_at and draft.ends_at <= bounds[1]:
                return None
    return Issue(
        "INSTRUCTOR_UNAVAILABLE",
        "Занятие вне окна доступности преподавателя",
        {"instructor_id": str(draft.instructor_id)},
    )
