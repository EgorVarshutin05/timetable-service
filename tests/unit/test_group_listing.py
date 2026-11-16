"""FR-16: query contract and read-only orchestration without PostgreSQL."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.main import create_app
from app.models import Group
from app.repositories import catalog
from tests.unit.test_catalog_api import CONTRACT, DATA
from tests.unit.test_catalog_api import catalog_client as catalog_client


@pytest.mark.parametrize(
    ("params", "limit", "offset"),
    [({}, 50, 0), ({"limit": "200"}, 200, 0), ({"limit": "1", "offset": "2"}, 1, 2)],
)
async def test_page_contract_and_snapshot(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, str],
    limit: int,
    offset: int,
) -> None:
    client, session = catalog_client
    # SQL возвращает уже отсортированный slice; API не теряет total или порядок.
    rows = [Group(**item) for item in [DATA["groups"][1], DATA["groups"][2], DATA["groups"][0]]]
    fetch = AsyncMock(return_value=(rows[offset : offset + limit], 3))
    monkeypatch.setattr(catalog, "get_groups_page", fetch)
    response = await client.get("/groups", params=params)
    assert response.status_code == 200
    CONTRACT.assert_schema(response.json(), "GroupPage")
    assert response.json() == {
        "items": [
            {"id": str(row.id), "name": row.name, "size": row.size}
            for row in rows[offset : offset + limit]
        ],
        "total": 3,
    }
    fetch.assert_awaited_once_with(session, limit, offset)
    session.connection.assert_awaited_once_with(
        execution_options={"isolation_level": "REPEATABLE READ"}
    )
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "-1"},
        {"offset": "-1"},
        {"limit": "oops"},
        {"offset": "1.5"},
        {"limit": ""},
    ],
)
async def test_invalid_pagination_is_422_before_sql(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock],
    params: dict[str, str],
) -> None:
    client, session = catalog_client
    response = await client.get("/groups", params=params)
    fields = CONTRACT.assert_error(response, 422, "VALIDATION_ERROR")["fields"]
    assert set(fields) == set(params)
    session.connection.assert_not_awaited()
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()


async def test_empty_page_keeps_total(
    catalog_client: tuple[httpx.AsyncClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "get_groups_page", AsyncMock(return_value=([], 3)))
    response = await catalog_client[0].get("/groups?offset=999")
    assert response.json() == {"items": [], "total": 3}


def test_generated_pagination_documents_original_defaults() -> None:
    operation = create_app().openapi()["paths"]["/groups"]["get"]
    params = {item["name"]: item["schema"] for item in operation["parameters"]}
    assert params["limit"] == {
        "default": 50,
        "minimum": 1,
        "maximum": 200,
        "title": "Limit",
        "type": "integer",
    }
    assert params["offset"]["default"] == params["offset"]["minimum"] == 0
