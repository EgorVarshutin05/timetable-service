"""Начальная ревизия: схема пуста.

Revision ID: 0001
Revises:
Create Date: 2026-11-09 00:00:00 UTC

Первая предметная миграция создаётся командой ``just migration "<описание>"`` и ревизуется:
autogenerate не умеет выражать ограничения уровня СУБД сверх базовых (их добавляют вручную
через ``op.execute`` или ``op.create_*``), не создаёт расширения PostgreSQL и не видит моделей,
не импортированных в ``migrations/env.py``.
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Схема пуста: ревизия фиксирует точку отсчёта."""


def downgrade() -> None:
    """Нечего откатывать."""
