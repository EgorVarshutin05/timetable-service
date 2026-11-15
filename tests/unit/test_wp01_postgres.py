"""Проверки настоящих ограничений и транзакций PostgreSQL 16.

WP01_TEST_DATABASE_URL включает эти тесты. Каждый тест использует отдельную временную схему,
не очищая таблицы public. У роли должны быть права CREATE SCHEMA.
"""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateSchema, DropSchema

from app import main
from app.db import Base, make_session_factory, session_scope
from app.models import Group, Lesson
from app.seed import load_seed, read_all, reset_data
from app.settings import Settings

DATABASE_URL = os.getenv("WP01_TEST_DATABASE_URL")
L1 = UUID("3d000000-0000-4000-8000-000000000001")
G1 = UUID("9a000000-0000-4000-8000-000000000001")


@dataclass
class Database:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    if DATABASE_URL is None:
        pytest.skip("Укажите WP01_TEST_DATABASE_URL для проверок PostgreSQL")
    schema = f"wp01_{uuid4().hex}"
    engine = create_async_engine(
        DATABASE_URL, connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}}
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.run_sync(Base.metadata.create_all)
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await load_seed(session, read_all())
        yield Database(engine, factory)
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await engine.dispose()


async def test_seed_is_idempotent_and_preserves_existing_data(database: Database) -> None:
    async with session_scope(database.factory) as session:
        await session.execute(update(Group).where(Group.id == G1).values(name="Изменено"))
    data = read_all()
    data["lessons"].reverse()  # FK переноса не зависит от порядка записей seed.
    for _ in range(2):
        async with session_scope(database.factory) as session:
            await load_seed(session, data)
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Group)) == 3
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 8
        assert await session.scalar(select(Group.name).where(Group.id == G1)) == "Изменено"


@pytest.mark.parametrize(
    "changes",
    [
        {"ends_at": datetime(2026, 12, 1, 5, tzinfo=UTC)},
        {"ends_at": datetime(2026, 12, 1, 4, tzinfo=UTC)},
        {"ends_at": datetime(2026, 12, 1, 5, 29, tzinfo=UTC)},
        {"ends_at": datetime(2026, 12, 1, 13, 1, tzinfo=UTC)},
        {"classroom_id": None},
        {"format": "ONLINE"},
        {"status": "UNKNOWN"},
        {"format": "UNKNOWN"},
        {"group_id": uuid4()},
        {"rescheduled_from_id": uuid4()},
        {"rescheduled_from_id": L1},
        {"rescheduled_from_id": UUID("3d000000-0000-4000-8000-000000000006")},
    ],
)
async def test_database_rejects_invalid_lesson(
    database: Database, changes: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with session_scope(database.factory) as session:
            await session.execute(update(Lesson).where(Lesson.id == L1).values(**changes))


async def test_database_rejects_empty_group(database: Database) -> None:
    with pytest.raises(IntegrityError):
        async with session_scope(database.factory) as session:
            await session.execute(update(Group).where(Group.id == G1).values(size=0))


@pytest.mark.parametrize("duration", [timedelta(minutes=30), timedelta(hours=8)])
async def test_database_accepts_duration_boundaries(
    database: Database, duration: timedelta
) -> None:
    async with session_scope(database.factory) as session:
        await session.execute(
            update(Lesson)
            .where(Lesson.id == L1)
            .values(ends_at=datetime(2026, 12, 1, 5, tzinfo=UTC) + duration)
        )


async def test_reset_endpoint_restores_seed_and_preserves_clock(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "make_engine", lambda _: database.engine)
    context = main.build_context(Settings(test_mode=True))
    frozen = datetime(2026, 11, 30, 3, tzinfo=UTC)
    context.clock.freeze(frozen)
    async with session_scope(database.factory) as session:
        await session.execute(update(Group).where(Group.id == G1).values(name="Изменено"))
        session.add(Group(id=uuid4(), name="Лишняя группа", size=1))
    app = main.create_app()
    app.state.context = context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post("/__test/reset")
            assert response.status_code == 204
            assert response.content == b""
            assert context.clock.now() == frozen
            assert context.clock.is_frozen
        group = await client.get(f"/groups/{G1}")
        lesson = await client.get(f"/lessons/{L1}")
    assert group.json() == read_all()["groups"][0]
    assert lesson.json() == read_all()["lessons"][0] | {"series_id": None}
    async with database.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Group)) == 3


async def test_failed_reset_rolls_back_truncate(database: Database, tmp_path: Path) -> None:
    data = read_all()
    data["lessons"][0]["classroom_id"] = None
    for name, records in data.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(records), encoding="utf-8")
    async with session_scope(database.factory) as session:
        await session.execute(update(Group).where(Group.id == G1).values(name="До сброса"))
    with pytest.raises(IntegrityError):
        await reset_data(database.factory, tmp_path)
    async with database.factory() as session:
        assert await session.scalar(select(Group.name).where(Group.id == G1)) == "До сброса"
        assert await session.scalar(select(func.count()).select_from(Lesson)) == 8


@pytest.mark.parametrize("seed_on_start", [False, True])
async def test_seed_on_start(
    database: Database, monkeypatch: pytest.MonkeyPatch, seed_on_start: bool
) -> None:
    async with session_scope(database.factory) as session:
        await session.execute(delete(Lesson))
        await session.execute(delete(Group))
    settings = Settings(test_mode=True, seed_on_start=seed_on_start)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "make_engine", lambda _: database.engine)
    app = main.create_app()
    async with app.router.lifespan_context(app), database.factory() as session:
        expected = 8 if seed_on_start else 0
        assert await session.scalar(select(func.count()).select_from(Lesson)) == expected
