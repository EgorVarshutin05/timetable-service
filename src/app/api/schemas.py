"""Схемы чтения из openapi.yaml; все даты сериализуются единым помощником."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

from app.clock import to_iso_z
from app.domain.lesson import LessonFormat, LessonStatus


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
