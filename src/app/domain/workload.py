"""FR-15: ISO-недели и дневная нагрузка без HTTP, ORM или собственных часов."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from app.domain.people import InstructorProfile
from app.domain.scheduling import (
    ACTIVE_STATUSES,
    InvalidLesson,
    Issue,
    ScheduledLesson,
    daily_hours,
)

_WEEK_PATTERN = re.compile(r"\d{4}-W\d{2}", re.ASCII)


@dataclass(frozen=True, slots=True)
class IsoWeek:
    value: str
    monday: date

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(self.monday + timedelta(days=index) for index in range(7))


@dataclass(frozen=True, slots=True)
class WorkloadDay:
    date: date
    hours: Decimal
    exceeded: bool


@dataclass(frozen=True, slots=True)
class WeeklyWorkload:
    instructor_id: UUID
    week: str
    max_hours_per_day: Decimal
    days: tuple[WorkloadDay, ...]


def _invalid_week() -> InvalidLesson:
    return InvalidLesson(
        Issue(
            "VALIDATION_ERROR",
            "Невалидная ISO-неделя",
            {
                "fields": {"week": "Ожидается существующая ISO-неделя в формате YYYY-Www"},
            },
        )
    )


def parse_week(value: str) -> IsoWeek:
    if _WEEK_PATTERN.fullmatch(value) is None:
        raise _invalid_week()
    try:
        monday = date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
        # Требуются все 7 дат и исключительная верхняя граница, без overflow в I/O.
        monday + timedelta(days=7)
    except (ValueError, OverflowError) as exc:
        raise _invalid_week() from exc
    return IsoWeek(value, monday)


def week_bounds(week: IsoWeek, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    try:
        start = datetime.combine(week.monday, time.min, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(week.monday + timedelta(days=7), time.min, tzinfo=zone).astimezone(
            UTC
        )
    except (ValueError, OverflowError) as exc:
        raise _invalid_week() from exc
    return start, end


def summarize_workload(
    profile: InstructorProfile, week: IsoWeek, lessons: Iterable[ScheduledLesson]
) -> WeeklyWorkload:
    start, end = week_bounds(week, profile.timezone)
    totals = dict.fromkeys(week.days, Decimal(0))
    for lesson in lessons:
        if lesson.instructor_id != profile.id or lesson.status not in ACTIVE_STATUSES:
            continue
        lo, hi = max(start, lesson.starts_at), min(end, lesson.ends_at)
        if lo < hi:
            for day, hours in daily_hours(lo, hi, profile.timezone).items():
                if day in totals:
                    totals[day] += hours
    return WeeklyWorkload(
        profile.id,
        week.value,
        profile.max_hours_per_day,
        tuple(
            WorkloadDay(day, hours, hours > profile.max_hours_per_day)
            for day, hours in totals.items()
        ),
    )
