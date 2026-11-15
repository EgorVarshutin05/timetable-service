"""Nullable membership in a lesson series; existing lessons remain ungrouped.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lessons", sa.Column("series_id", sa.Uuid(), nullable=True))
    op.create_index("ix_lessons_series_id", "lessons", ["series_id"])


def downgrade() -> None:
    op.drop_index("ix_lessons_series_id", table_name="lessons")
    op.drop_column("lessons", "series_id")
