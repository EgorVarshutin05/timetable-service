"""Схемы чтения из openapi.yaml; все даты сериализуются единым помощником."""

import re
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.clock import to_iso_z
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.listing import DEFAULT_LIMIT, MAX_LIMIT, LessonFilters
from app.domain.scheduling import DraftLesson
from app.domain.series import SeriesPlan, Weekday
from app.errors import ApiError, classify_validation_error

_OFFSET_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def parse_iso_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise PydanticCustomError("datetime_type", "Ожидается строка ISO 8601")
    if _OFFSET_DATETIME.fullmatch(value) is None:
        raise ValueError("Ожидается ISO 8601 со смещением часового пояса")
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).astimezone(UTC)


class LessonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: UUID
    instructor_id: UUID
    classroom_id: UUID | None = None
    format: LessonFormat
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @field_validator("format", mode="before")
    @classmethod
    def require_format_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Ожидается строка формата занятия")
        return value

    @field_validator("starts_at", "ends_at", mode="before")
    @classmethod
    def parse_datetime(cls, value: object) -> datetime:
        return parse_iso_datetime(value)

    def to_draft(self) -> DraftLesson:
        return DraftLesson(
            self.group_id,
            self.instructor_id,
            self.classroom_id,
            self.format,
            self.starts_at,
            self.ends_at,
        )


def _patch_json_schema(schema: dict[str, Any]) -> None:
    # Документируем нормативные типы, хотя значения валидируются отложенно после статуса.
    source = LessonCreate.model_json_schema()
    for name in schema["properties"]:
        value = dict(source["properties"][name])
        if "$ref" in value:
            value = dict(source["$defs"][value["$ref"].rsplit("/", 1)[-1]])
        value.pop("default", None)
        schema["properties"][name] = value
    schema["minProperties"] = 1


class LessonPatch(BaseModel):
    """Проверяет форму JSON сейчас; значения применяет после проверки статуса в service."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_patch_json_schema)

    starts_at: str | None = None
    ends_at: str | None = None
    format: str | None = None
    instructor_id: str | None = None
    classroom_id: str | None = None

    @field_validator(
        "starts_at", "ends_at", "format", "instructor_id", "classroom_id", mode="before"
    )
    @classmethod
    def require_string_or_classroom_null(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            if info.field_name == "classroom_id":
                return None
            raise ValueError("Поле не может быть null")
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Ожидается строка")
        return value

    @model_validator(mode="after")
    def require_changes(self) -> "LessonPatch":
        if not self.model_fields_set:
            raise ValueError("Нужно передать хотя бы одно изменяемое поле")
        return self

    def apply(self, current: DraftLesson) -> DraftLesson:
        raw: dict[str, Any] = {
            "group_id": current.group_id,
            "instructor_id": current.instructor_id,
            "classroom_id": current.classroom_id,
            "format": current.format,
            "starts_at": current.starts_at.isoformat(),
            "ends_at": current.ends_at.isoformat(),
        }
        # exclude_unset использует model_fields_set: null остаётся, пропущенные поля исключаются.
        raw.update(self.model_dump(exclude_unset=True))
        try:
            return LessonCreate.model_validate(raw).to_draft()
        except ValidationError as exc:
            status, code, details = classify_validation_error(exc.errors())
            raise ApiError(status, code, "Невалидные данные PATCH", details) from exc


class RescheduleRequest(LessonPatch):
    """Интервал обязателен; остальные поля наследуются, включая различение null."""

    starts_at: str
    ends_at: str


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500, strict=True)


class LessonSeriesCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: UUID
    instructor_id: UUID
    classroom_id: UUID | None = None
    format: LessonFormat
    weekdays: list[Weekday] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    count: int = Field(ge=1, le=50, strict=True)
    first_starts_at: AwareDatetime
    duration_minutes: int = Field(ge=30, le=480, strict=True)

    @field_validator("count", "duration_minutes", mode="before")
    @classmethod
    def require_json_integer(cls, value: object) -> int:
        # JSON Schema считает 2 и 2.0 целыми; строки и boolean целыми не являются.
        if type(value) is int:
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise PydanticCustomError("int_type", "Ожидается целое число JSON")

    @field_validator("format", mode="before")
    @classmethod
    def require_format_string(cls, value: object) -> object:
        return LessonCreate.require_format_string(value)

    @field_validator("first_starts_at", mode="before")
    @classmethod
    def parse_first_datetime(cls, value: object) -> datetime:
        return parse_iso_datetime(value)

    @field_validator("weekdays", mode="before")
    @classmethod
    def require_weekday_strings(cls, value: object) -> object:
        if not isinstance(value, list):
            raise PydanticCustomError("list_type", "Ожидается массив дней недели")
        if any(not isinstance(day, str) for day in value):
            raise PydanticCustomError("string_type", "Дни недели должны быть строками")
        return value

    @field_validator("weekdays")
    @classmethod
    def unique_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        if len(set(value)) != len(value):
            raise ValueError("Дни недели не должны повторяться")
        return value

    def to_plan(self) -> SeriesPlan:
        return SeriesPlan(
            self.group_id,
            self.instructor_id,
            self.classroom_id,
            self.format,
            tuple(self.weekdays),
            self.count,
            self.first_starts_at,
            self.duration_minutes,
        )


class LessonQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_id: UUID | None = None
    instructor_id: UUID | None = None
    classroom_id: UUID | None = None
    status: LessonStatus | None = None
    starts_from: AwareDatetime | None = Field(default=None, alias="from")
    starts_to: AwareDatetime | None = Field(default=None, alias="to")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)

    @field_validator("starts_from", "starts_to", mode="before")
    @classmethod
    def parse_period(cls, value: object) -> datetime | None:
        return None if value is None else parse_iso_datetime(value)

    def to_filters(self) -> LessonFilters:
        return LessonFilters(
            self.group_id,
            self.instructor_id,
            self.classroom_id,
            self.status,
            self.starts_from,
            self.starts_to,
            self.limit,
            self.offset,
        )


def list_query_parameters() -> list[dict[str, Any]]:
    schema = LessonQuery.model_json_schema(ref_template="#/components/schemas/{model}")
    return [
        {"name": name, "in": "query", "required": False, "schema": value}
        for name, value in schema["properties"].items()
    ]


class GroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    size: int = Field(ge=1)


class LessonResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    group_id: UUID
    instructor_id: UUID
    classroom_id: UUID | None
    format: LessonFormat
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    status: LessonStatus
    rescheduled_from_id: UUID | None
    cancel_reason: str | None
    series_id: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_serializer("starts_at", "ends_at", "created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return to_iso_z(value)


class IssueResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any]


class WarningResponse(IssueResponse):
    code: Literal["WORKLOAD_SOFT_LIMIT", "INSTRUCTOR_UNAVAILABLE"]


class ViolationResponse(IssueResponse):
    code: Literal["CLASSROOM_TOO_SMALL", "SCHEDULE_CONFLICT"]


class LessonWithWarnings(BaseModel):
    lesson: LessonResponse
    warnings: list[WarningResponse]


class LessonSeriesResult(BaseModel):
    series_id: UUID
    items: list[LessonWithWarnings]


class WorkloadDayResponse(BaseModel):
    date: date
    hours: float = Field(ge=0)
    exceeded: bool


class WorkloadResponse(BaseModel):
    instructor_id: UUID
    week: str
    max_hours_per_day: float
    days: list[WorkloadDayResponse] = Field(min_length=7, max_length=7)


class LessonPage(BaseModel):
    items: list[LessonResponse]
    total: int = Field(ge=0)


class ValidationResult(BaseModel):
    ok: bool
    violations: list[ViolationResponse]
    warnings: list[WarningResponse]
