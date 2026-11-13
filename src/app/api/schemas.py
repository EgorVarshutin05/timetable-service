"""Схемы чтения из openapi.yaml; все даты сериализуются единым помощником."""

import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic_core import PydanticCustomError

from app.clock import to_iso_z
from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.scheduling import DraftLesson

_OFFSET_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


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
        if not isinstance(value, str):
            raise PydanticCustomError("datetime_type", "Ожидается строка ISO 8601")
        if _OFFSET_DATETIME.fullmatch(value) is None:
            raise ValueError("Ожидается ISO 8601 со смещением часового пояса")
        return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).astimezone(UTC)

    def to_draft(self) -> DraftLesson:
        return DraftLesson(
            self.group_id,
            self.instructor_id,
            self.classroom_id,
            self.format,
            self.starts_at,
            self.ends_at,
        )


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


class ValidationResult(BaseModel):
    ok: bool
    violations: list[ViolationResponse]
    warnings: list[WarningResponse]
