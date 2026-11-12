"""Храним только группы и занятия; преподаватели и аудитории принадлежат people-stub."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.domain.lesson import LessonFormat, LessonStatus


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (CheckConstraint("size >= 1", name="ck_groups_size"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    size: Mapped[int]


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_lessons_interval"),
        CheckConstraint(
            "ends_at - starts_at BETWEEN INTERVAL '30 minutes' AND INTERVAL '8 hours'",
            name="ck_lessons_duration",
        ),
        CheckConstraint(
            "(format = 'IN_PERSON' AND classroom_id IS NOT NULL) OR "
            "(format = 'ONLINE' AND classroom_id IS NULL)",
            name="ck_lessons_classroom_format",
        ),
        CheckConstraint("rescheduled_from_id <> id", name="ck_lessons_not_self_rescheduled"),
        UniqueConstraint("rescheduled_from_id", name="uq_lessons_rescheduled_from"),
        Index("ix_lessons_group_interval", "group_id", "starts_at", "ends_at"),
        Index("ix_lessons_instructor_interval", "instructor_id", "starts_at", "ends_at"),
        Index("ix_lessons_classroom_interval", "classroom_id", "starts_at", "ends_at"),
        Index("ix_lessons_starts_id", "starts_at", "id"),
        Index("ix_lessons_status_starts_id", "status", "starts_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    group_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", name="fk_lessons_group", ondelete="RESTRICT")
    )
    instructor_id: Mapped[UUID] = mapped_column(Uuid)
    classroom_id: Mapped[UUID | None] = mapped_column(Uuid)
    format: Mapped[LessonFormat] = mapped_column(
        Enum(LessonFormat, native_enum=False, create_constraint=True, name="lesson_format")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[LessonStatus] = mapped_column(
        Enum(LessonStatus, native_enum=False, create_constraint=True, name="lesson_status")
    )
    rescheduled_from_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "lessons.id",
            name="fk_lessons_rescheduled_from",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
