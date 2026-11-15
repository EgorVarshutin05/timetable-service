"""Seed: детерминированные фикстуры из ``seed/*.json`` (TASK.md, §3.5).

Каркас умеет читать файлы seed. Загрузка в БД (``load_seed``) появляется вместе с моделями:
её вызывают ``just seed`` (``python -m app.seed``), ``SEED_ON_START=1`` и ``POST /__test/reset``.
Загрузчик обязан быть идемпотентным (повторный запуск на уже засеянной БД не падает)
и обходить доменную валидацию: seed не должен зависеть от заглушки.
"""

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import make_engine, make_session_factory, session_scope
from app.domain.lesson import LessonFormat, LessonStatus
from app.models import Group, Lesson
from app.repositories.seed import clear_data, insert_missing
from app.settings import get_settings

# Корень репозитория: src/app/seed.py → parents[2]. В образе это /app (editable-установка пакета).
SEED_DIR = Path(__file__).resolve().parents[2] / "seed"

log = logging.getLogger(__name__)

SeedRecords = list[dict[str, Any]]


class SeedGroup(BaseModel):
    """Проверка формы seed, без запросов к people-stub и предметных правил."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    size: int = Field(ge=1)


class SeedLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @field_validator("starts_at", "ends_at", "created_at", "updated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


def read_seed_file(name: str, seed_dir: Path = SEED_DIR) -> SeedRecords:
    """Прочитать ``seed/<name>.json``: список объектов с фиксированными UUID."""
    path = seed_dir / f"{name}.json"
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        msg = f"{path}: ожидается список записей"
        raise ValueError(msg)
    records: SeedRecords = []
    for item in data:
        if not isinstance(item, dict):
            msg = f"{path}: каждая запись должна быть объектом"
            raise ValueError(msg)
        records.append(item)
    return records


def read_all(seed_dir: Path = SEED_DIR) -> dict[str, SeedRecords]:
    """Все файлы seed по имени файла без расширения."""
    return {
        path.stem: read_seed_file(path.stem, seed_dir) for path in sorted(seed_dir.glob("*.json"))
    }


async def load_seed(session: AsyncSession, data: dict[str, SeedRecords]) -> None:
    """Добавить отсутствующие UUID, не перезаписывая существующие записи; commit у вызывающего."""
    groups = [SeedGroup.model_validate(row).model_dump() for row in data["groups"]]
    lessons = [SeedLesson.model_validate(row).model_dump() for row in data["lessons"]]
    await insert_missing(session, Group, groups)
    await insert_missing(session, Lesson, lessons)


async def reset_data(factory: async_sessionmaker[AsyncSession], seed_dir: Path = SEED_DIR) -> None:
    """Очистить и восстановить данные одной транзакцией; часы не являются зависимостью."""
    data = await asyncio.to_thread(read_all, seed_dir)
    async with session_scope(factory) as session:
        await clear_data(session)
        await load_seed(session, data)


async def main() -> int:
    """``python -m app.seed``: загрузить seed в БД из ``DATABASE_URL``."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    data = await asyncio.to_thread(read_all)
    log.info("Прочитан seed: %s", {name: len(rows) for name, rows in data.items()})
    try:
        async with factory() as session:
            await load_seed(session, data)
            await session.commit()
    finally:
        await engine.dispose()
    log.info("Seed загружен")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
