"""Жизненный цикл HTTP-клиента и сохранение точек расширения WP-01."""

from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import main
from app.api import health
from app.broker import BrokerProbe
from app.clients.people import PeopleClient
from app.settings import Settings


@pytest.mark.parametrize("failure", ["none", "body", "seed"])
async def test_lifespan_closes_people_and_engine(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    settings = Settings(test_mode=True, seed_on_start=failure == "seed")
    context = main.build_context(settings)
    assert context.people_client is not None
    assert context.reset_state is not None
    client = context.people_client
    disposed: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def record_dispose(engine: AsyncEngine, close: bool = True) -> None:
        disposed.append(engine)
        await original_dispose(engine, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", record_dispose)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_context", lambda _: context)
    if failure == "seed":
        monkeypatch.setattr(main, "load_seed", AsyncMock(side_effect=RuntimeError("seed failed")))
    app = main.create_app()
    if failure == "none":
        async with app.router.lifespan_context(app):
            assert not client.is_closed
            paths = set(app.openapi()["paths"])
            assert {"/groups/{group_id}", "/lessons/{lesson_id}", "/__test/reset"} <= paths
    else:
        with pytest.raises(RuntimeError, match="failed"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("body failed")
    assert client.is_closed
    assert disposed == [context.engine]


async def test_readiness_does_not_call_people_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    async def fail_if_called(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("people-stub is down", request=request)

    settings = Settings(database_url="postgresql+asyncpg://wa:wa@127.0.0.1:1/unreachable")
    context = main.build_context(settings)
    database_probe = AsyncMock(return_value=False)
    monkeypatch.setattr(health, "check_database", database_probe)
    broker_probe = AsyncMock(spec=BrokerProbe)
    broker_probe.ping.return_value = False
    context.broker_probe = broker_probe
    assert context.people_client is not None
    await context.people_client.aclose()
    async with PeopleClient(settings, transport=httpx.MockTransport(fail_if_called)) as people:
        context.people_client = people
        app = main.create_app()
        app.state.context = context
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://service"
            ) as client:
                response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json() == {
                "status": "degraded",
                "db": "fail",
                "broker": "fail",
            }
            assert calls == []
            database_probe.assert_awaited_once_with(context.engine)
            broker_probe.ping.assert_awaited_once()
        finally:
            if context.broker_client is not None:
                await context.broker_client.aclose()
            await context.engine.dispose()
