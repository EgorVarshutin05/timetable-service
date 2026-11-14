"""Параметры выборки расписания, не зависящие от SQL и HTTP."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from app.domain.lesson import LessonStatus
from app.domain.scheduling import InvalidLesson, Issue

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class LessonFilters:
    group_id: UUID | None = None
    instructor_id: UUID | None = None
    classroom_id: UUID | None = None
    status: LessonStatus | None = None
    starts_from: datetime | None = None
    starts_to: datetime | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def validate_filters(filters: LessonFilters) -> LessonFilters:
    fields: dict[str, str] = {}
    if not 1 <= filters.limit <= MAX_LIMIT:
        fields["limit"] = "Допустимо от 1 до 200"
    if filters.offset < 0:
        fields["offset"] = "Смещение не может быть отрицательным"
    for name, moment in (("from", filters.starts_from), ("to", filters.starts_to)):
        if moment is not None and moment.utcoffset() is None:
            fields[name] = "Нужно смещение часового пояса"
    if fields:
        raise InvalidLesson(Issue("VALIDATION_ERROR", "Невалидные параметры", {"fields": fields}))
    filters = replace(
        filters,
        starts_from=filters.starts_from.astimezone(UTC)
        if filters.starts_from is not None
        else None,
        starts_to=filters.starts_to.astimezone(UTC) if filters.starts_to is not None else None,
    )
    if (
        filters.starts_from is not None
        and filters.starts_to is not None
        and filters.starts_from >= filters.starts_to
    ):
        raise InvalidLesson(
            Issue(
                "VALIDATION_ERROR",
                "Невалидный период",
                {"fields": {"to": "Должно быть строго позже from"}},
            )
        )
    return filters
