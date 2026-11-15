"""FR-04/FR-11: HTTP и контракт проверяются без PostgreSQL и брокера."""

from collections.abc import AsyncIterator
from datetime import timedelta, timezone
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.api.schemas import LessonResponse
from app.main import create_app
from app.models import Group, Lesson
from app.seed import SeedGroup, SeedLesson, read_all
from tests.e2e.contract import ContractChecker

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ContractChecker.from_file(ROOT / "openapi.yaml")
DATA = read_all()


@pytest.fixture
async def catalog_client() -> AsyncIterator[tuple[httpx.AsyncClient, AsyncMock]]:
    session = AsyncMock(spec=AsyncSession)

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    app = create_app()
    app.dependency_overrides[get_session] = fake_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, session


@pytest.mark.parametrize("index", range(3))
async def test_get_seed_group(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock], index: int
) -> None:
    client, session = catalog_client
    row = DATA["groups"][index]
    session.get.return_value = Group(**SeedGroup.model_validate(row).model_dump())
    response = await client.get(f"/groups/{row['id']}")
    assert response.status_code == 200
    CONTRACT.assert_schema(response.json(), "Group")
    assert response.json() == row


@pytest.mark.parametrize("index", range(8))
async def test_get_seed_lesson(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock], index: int
) -> None:
    client, session = catalog_client
    row = DATA["lessons"][index]
    session.get.return_value = Lesson(**SeedLesson.model_validate(row).model_dump())
    response = await client.get(f"/lessons/{row['id']}")
    assert response.status_code == 200
    CONTRACT.assert_schema(response.json(), "Lesson")
    assert response.json() == row | {"series_id": None}


@pytest.mark.parametrize("resource", ["groups", "lessons"])
async def test_missing_resource_is_404(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock], resource: str
) -> None:
    client, session = catalog_client
    session.get.return_value = None
    response = await client.get(f"/{resource}/00000000-0000-4000-8000-000000000099")
    CONTRACT.assert_error(response, 404, "NOT_FOUND")


@pytest.mark.parametrize("resource", ["groups", "lessons"])
async def test_malformed_uuid_is_400_before_database(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock], resource: str
) -> None:
    client, session = catalog_client
    response = await client.get(f"/{resource}/not-a-uuid")
    CONTRACT.assert_error(response, 400, "BAD_REQUEST")
    session.get.assert_not_awaited()


def test_response_normalizes_offset_and_drops_microseconds() -> None:
    seed = SeedLesson.model_validate(DATA["lessons"][0])
    seed.starts_at = seed.starts_at.astimezone(timezone(timedelta(hours=5))).replace(
        microsecond=123456
    )
    lesson = Lesson(**seed.model_dump())
    response = LessonResponse.model_validate(lesson).model_dump(mode="json")
    assert response["starts_at"] == "2026-12-01T05:00:00Z"
