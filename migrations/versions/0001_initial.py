"""Группы и занятия, ограничения и индексы.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.CheckConstraint("size >= 1", name="ck_groups_size"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "lessons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("instructor_id", sa.Uuid(), nullable=False),
        sa.Column("classroom_id", sa.Uuid(), nullable=True),
        sa.Column(
            "format",
            sa.Enum(
                "IN_PERSON",
                "ONLINE",
                native_enum=False,
                create_constraint=True,
                name="lesson_format",
            ),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PLANNED",
                "CONFIRMED",
                "IN_PROGRESS",
                "COMPLETED",
                "CANCELLED",
                "RESCHEDULED",
                native_enum=False,
                create_constraint=True,
                name="lesson_status",
            ),
            nullable=False,
        ),
        sa.Column("rescheduled_from_id", sa.Uuid(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="ck_lessons_interval"),
        sa.CheckConstraint(
            "ends_at - starts_at BETWEEN INTERVAL '30 minutes' AND INTERVAL '8 hours'",
            name="ck_lessons_duration",
        ),
        sa.CheckConstraint(
            "(format = 'IN_PERSON' AND classroom_id IS NOT NULL) OR "
            "(format = 'ONLINE' AND classroom_id IS NULL)",
            name="ck_lessons_classroom_format",
        ),
        sa.CheckConstraint("rescheduled_from_id <> id", name="ck_lessons_not_self_rescheduled"),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name="fk_lessons_group", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rescheduled_from_id"],
            ["lessons.id"],
            name="fk_lessons_rescheduled_from",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rescheduled_from_id", name="uq_lessons_rescheduled_from"),
    )
    for resource in ("group", "instructor", "classroom"):
        op.create_index(
            f"ix_lessons_{resource}_interval", "lessons", [f"{resource}_id", "starts_at", "ends_at"]
        )
    op.create_index("ix_lessons_starts_id", "lessons", ["starts_at", "id"])
    op.create_index("ix_lessons_status_starts_id", "lessons", ["status", "starts_at", "id"])


def downgrade() -> None:
    op.drop_table("lessons")
    op.drop_table("groups")
