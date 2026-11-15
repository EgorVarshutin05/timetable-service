"""FR-14: чистая генерация по UTC-дням недели и ранние BR-01/02."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from app.domain.lesson import LessonFormat
from app.domain.scheduling import DraftLesson, InvalidLesson, Issue, validate_early


class Weekday(StrEnum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


_WEEKDAYS = {day: index for index, day in enumerate(Weekday)}


@dataclass(frozen=True, slots=True)
class SeriesPlan:
    group_id: UUID
    instructor_id: UUID
    classroom_id: UUID | None
    format: LessonFormat
    weekdays: tuple[Weekday, ...]
    count: int
    first_starts_at: datetime
    duration_minutes: int


def _invalid(field: str, message: str) -> InvalidLesson:
    return InvalidLesson(
        Issue(
            "VALIDATION_ERROR",
            "Невалидные данные серии",
            {
                "fields": {field: message},
            },
        )
    )


def generate_series(plan: SeriesPlan) -> tuple[DraftLesson, ...]:
    if type(plan.count) is not int or not 1 <= plan.count <= 50:
        raise _invalid("count", "Количество должно быть от 1 до 50")
    if type(plan.duration_minutes) is not int or not 30 <= plan.duration_minutes <= 480:
        raise _invalid("duration_minutes", "Длительность должна быть от 30 до 480 минут")
    if (
        not plan.weekdays
        or len(set(plan.weekdays)) != len(plan.weekdays)
        or any(day not in _WEEKDAYS for day in plan.weekdays)
    ):
        raise _invalid("weekdays", "Нужен непустой список уникальных дней MON..SUN")
    if plan.first_starts_at.utcoffset() is None:
        raise _invalid("first_starts_at", "Время должно содержать смещение часового пояса")
    selected = {_WEEKDAYS[day] for day in plan.weekdays}
    result: list[DraftLesson] = []
    try:
        current = plan.first_starts_at.astimezone(UTC)
        while len(result) < plan.count:
            if current.weekday() in selected:
                draft = DraftLesson(
                    plan.group_id,
                    plan.instructor_id,
                    plan.classroom_id,
                    plan.format,
                    current,
                    current + timedelta(minutes=plan.duration_minutes),
                )
                try:
                    result.append(validate_early(draft))
                except InvalidLesson as exc:
                    issue = exc.issue
                    raise InvalidLesson(
                        Issue(
                            issue.code,
                            issue.message,
                            issue.details | {"occurrence_index": len(result)},
                        )
                    ) from exc
                if len(result) == plan.count:
                    break
            current += timedelta(days=1)
    except OverflowError as exc:
        raise _invalid("first_starts_at", "Даты серии выходят за поддерживаемый диапазон") from exc
    return tuple(result)
