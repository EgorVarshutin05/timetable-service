"""AsyncAPI, AMQP/Kafka metadata, ленивое подключение и readiness без сети."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import aio_pika
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import broker, main
from app.api import health
from app.broker import KafkaBroker, RabbitBroker
from app.clock import AdjustableClock
from app.domain import events
from app.domain.events import LessonEvent
from app.domain.lesson import LessonFormat, LessonStatus
from app.repositories import scheduling
from app.services.lessons import LessonService, PeopleSource
from app.settings import Settings
from tests.unit.event_helpers import assert_event
from tests.unit.scheduling_data import DRAFT, I2, L1
from tests.unit.test_lesson_commands_api import CONTRACT
from tests.unit.test_lesson_listing_and_patch import row

NOW = datetime(2026, 11, 30, 3, tzinfo=UTC)
NEW_ID = UUID("4e000000-0000-4000-8000-000000000001")
NEW = replace(DRAFT, instructor_id=I2, format=LessonFormat.ONLINE, classroom_id=None)


def sample_events() -> list[LessonEvent]:
    return [
        events.confirmed(L1, DRAFT, NOW),
        events.cancelled(L1, DRAFT, LessonStatus.CONFIRMED, "Причина", NOW),
        events.rescheduled(L1, NEW_ID, DRAFT, NEW, NOW),
    ]


@pytest.mark.parametrize("event", sample_events())
def test_all_event_schemas_and_envelopes(event: LessonEvent) -> None:
    body = event.to_dict()
    assert_event(body)
    assert set(body) == {
        "event_id",
        "event_type",
        "occurred_at",
        "idempotency_key",
        "producer",
        "version",
        "payload",
    }
    assert UUID(body["event_id"]).version == 4
    assert body["occurred_at"] == "2026-11-30T03:00:00Z"
    assert body["idempotency_key"].endswith(":" + str(L1))
    assert body["producer"] == "timetable-service"
    assert body["version"] == 1
    if event.event_type == "LessonRescheduled":
        assert body["payload"]["instructor_id"] == str(DRAFT.instructor_id)
        assert body["payload"]["new"]["instructor_id"] == str(I2)
        assert body["payload"]["new"]["classroom_id"] is None
    elif event.event_type == "LessonCancelled":
        assert body["payload"]["previous_status"] == "CONFIRMED"
        assert body["payload"]["reason"] == "Причина"


@pytest.mark.parametrize("status", [LessonStatus.PLANNED, LessonStatus.CONFIRMED])
@pytest.mark.parametrize("reason", [None, "", "Причина"])
def test_cancellation_nullable_reason_and_previous_status(
    status: LessonStatus, reason: str | None
) -> None:
    body = events.cancelled(L1, DRAFT, status, reason, NOW).to_dict()
    assert_event(body)
    assert body["payload"]["previous_status"] == status.value
    assert body["payload"]["reason"] == reason


@pytest.fixture
def amqp(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, MagicMock, MagicMock, MagicMock]:
    exchange = MagicMock()
    exchange.publish = AsyncMock()
    channel = MagicMock(is_closed=False)
    channel.declare_exchange = AsyncMock(return_value=exchange)
    connection = MagicMock(is_closed=False)
    connection.channel = AsyncMock(return_value=channel)
    connection.close = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(aio_pika, "connect", connect)
    return connect, connection, channel, exchange


@pytest.mark.parametrize("event", sample_events())
async def test_rabbit_routing_and_wire_properties(
    amqp: tuple[AsyncMock, MagicMock, MagicMock, MagicMock], event: LessonEvent
) -> None:
    connect, connection, channel, exchange = amqp
    client = RabbitBroker(Settings())
    connect.assert_not_awaited()
    await client.publish(event)
    connection.channel.assert_awaited_once_with(publisher_confirms=True)
    assert channel.declare_exchange.await_args.args == ("wa.events", aio_pika.ExchangeType.TOPIC)
    assert channel.declare_exchange.await_args.kwargs["durable"] is True
    call = exchange.publish.await_args
    message = call.args[0]
    assert json.loads(message.body) == event.to_dict()
    assert message.content_type == "application/json"
    assert message.message_id == str(event.event_id)
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert call.kwargs["routing_key"] == f"timetable.{event.event_type}"
    await client.aclose()
    connection.close.assert_awaited_once()


async def test_rabbit_readiness_uses_rpc_and_recovers(
    amqp: tuple[AsyncMock, MagicMock, MagicMock, MagicMock],
) -> None:
    connect, connection, channel, _ = amqp
    client = RabbitBroker(Settings())
    connect.side_effect = [OSError("down"), connection, connection]
    assert not await client.ping()
    assert await client.ping()
    assert channel.declare_exchange.await_args.kwargs["passive"] is True
    before = channel.declare_exchange.await_count
    assert await client.ping()
    assert channel.declare_exchange.await_count == before + 1
    channel.declare_exchange.side_effect = OSError("lost")
    assert not await client.ping()
    connection.close.assert_awaited_once()
    channel.declare_exchange.side_effect = None
    assert await client.ping()
    assert connect.await_count == 3
    await client.aclose()
    assert not await client.ping()


async def test_rabbit_concurrent_first_calls_share_one_connection(
    amqp: tuple[AsyncMock, MagicMock, MagicMock, MagicMock],
) -> None:
    connect, _, _, exchange = amqp
    client = RabbitBroker(Settings())
    await asyncio.gather(*(client.publish(event) for event in sample_events()))
    connect.assert_awaited_once()
    assert exchange.publish.await_count == 3
    await client.aclose()


@pytest.mark.parametrize("failure", [OSError("publish failed"), asyncio.CancelledError()])
async def test_rabbit_failed_publish_cleans_up_without_replay(
    amqp: tuple[AsyncMock, MagicMock, MagicMock, MagicMock], failure: BaseException
) -> None:
    _, connection, _, exchange = amqp
    client = RabbitBroker(Settings())
    exchange.publish.side_effect = failure
    with pytest.raises(type(failure)):
        await client.publish(sample_events()[0])
    assert exchange.publish.await_count == 1
    connection.close.assert_awaited_once()
    exchange.publish.side_effect = None
    await client.publish(sample_events()[1])
    await client.aclose()


async def test_rabbit_dead_channel_is_reopened(
    amqp: tuple[AsyncMock, MagicMock, MagicMock, MagicMock],
) -> None:
    connect, _, channel, _ = amqp
    client = RabbitBroker(Settings())
    await client.publish(sample_events()[0])
    channel.is_closed = True
    await client.publish(sample_events()[1])
    assert connect.await_count == 2
    await client.aclose()


@pytest.fixture
def kafka(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    producer = MagicMock(start=AsyncMock(), stop=AsyncMock(), send_and_wait=AsyncMock())
    factory = MagicMock(return_value=producer)
    admin = MagicMock(start=AsyncMock(), close=AsyncMock(), describe_cluster=AsyncMock())
    monkeypatch.setattr(broker, "AIOKafkaProducer", factory)
    monkeypatch.setattr(broker, "AIOKafkaAdminClient", MagicMock(return_value=admin))
    return factory, producer, admin


@pytest.mark.parametrize("event", sample_events())
async def test_kafka_wire_and_original_aggregate_key(
    kafka: tuple[MagicMock, MagicMock, MagicMock], event: LessonEvent
) -> None:
    factory, producer, _ = kafka
    client = KafkaBroker(Settings(broker="kafka", broker_url="kafka:9092"))
    factory.assert_not_called()
    await client.publish(event)
    call = producer.send_and_wait.await_args
    assert call.args == ("wa.timetable.events",)
    assert call.kwargs["key"] == str(L1).encode()
    assert call.kwargs["headers"] == [("event_type", event.event_type.encode())]
    assert json.loads(call.kwargs["value"]) == event.to_dict()
    assert factory.call_args.kwargs["acks"] == "all"
    await client.aclose()
    producer.stop.assert_awaited_once()


async def test_kafka_ping_failure_cleanup_and_recovery(
    kafka: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    _, producer, admin = kafka
    client = KafkaBroker(Settings(broker="kafka"))
    admin.start.side_effect = OSError("down")
    assert not await client.ping()
    admin.close.assert_awaited_once()
    admin.start.side_effect = None
    assert await client.ping()
    admin.describe_cluster.assert_awaited_once()
    producer.send_and_wait.side_effect = OSError("send failed")
    with pytest.raises(OSError, match="send failed"):
        await client.publish(sample_events()[0])
    producer.stop.assert_awaited_once()
    await client.aclose()
    assert not await client.ping()


@pytest.mark.parametrize(
    ("db_ok", "broker_ok"), [(True, True), (True, False), (False, True), (False, False)]
)
async def test_ready_exact_contract_and_live_independent(
    monkeypatch: pytest.MonkeyPatch, db_ok: bool, broker_ok: bool
) -> None:
    context = main.build_context(Settings(test_mode=True))
    probe = AsyncMock(return_value=broker_ok)
    context.broker_probe = MagicMock(ping=probe)
    monkeypatch.setattr(health, "check_database", AsyncMock(return_value=db_ok))
    app = main.create_app()
    app.state.context = context
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/ready")
            expected = "ok" if db_ok and broker_ok else "degraded"
            assert response.status_code == (200 if expected == "ok" else 503)
            assert response.json() == {
                "status": expected,
                "db": "ok" if db_ok else "fail",
                "broker": "ok" if broker_ok else "fail",
            }
            CONTRACT.assert_schema(response.json(), "Readiness")
            assert (await client.get("/health/live")).json() == {"status": "ok"}
    finally:
        assert context.people_client is not None
        assert context.broker_client is not None
        await context.people_client.aclose()
        await context.broker_client.aclose()
        await context.engine.dispose()


@pytest.mark.parametrize("commit_fails", [False, True])
@pytest.mark.parametrize("operation", ["confirm", "cancel"])
async def test_publish_only_after_successful_commit(
    monkeypatch: pytest.MonkeyPatch, commit_fails: bool, operation: str
) -> None:
    session = AsyncMock(spec=AsyncSession)
    factory = MagicMock(spec=async_sessionmaker)
    factory.return_value.__aenter__.return_value = session
    item = row()
    monkeypatch.setattr(scheduling, "get_lesson", AsyncMock(return_value=item))
    published: list[LessonEvent] = []

    class Publisher:
        async def publish(self, event: LessonEvent) -> None:
            session.commit.assert_awaited_once()
            assert event.occurred_at == NOW
            assert_event(event.to_dict())
            published.append(event)

    clock = AdjustableClock()
    clock.freeze(NOW)
    service = LessonService(
        cast(async_sessionmaker[AsyncSession], factory),
        cast(PeopleSource, AsyncMock()),
        clock,
        Publisher(),
    )
    if commit_fails:
        session.commit.side_effect = RuntimeError("commit failed")
        with pytest.raises(RuntimeError, match="commit failed"):
            await getattr(service, operation)(L1)
        assert published == []
        session.rollback.assert_awaited_once()
    else:
        await getattr(service, operation)(L1)
        assert len(published) == 1


@pytest.mark.parametrize("failure", ["none", "seed", "body"])
async def test_lifespan_closes_broker_even_on_startup_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    settings = Settings(test_mode=True, seed_on_start=failure == "seed")
    context = main.build_context(settings)
    fake = AsyncMock()
    context.broker_client = fake
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_context", lambda _: context)
    monkeypatch.setattr(main, "load_seed", AsyncMock(side_effect=RuntimeError("seed failed")))
    app = main.create_app()
    if failure == "none":
        async with app.router.lifespan_context(app):
            fake.ping.assert_not_awaited()
    else:
        with pytest.raises(RuntimeError, match="failed"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("body failed")
    fake.aclose.assert_awaited_once()


@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_readiness_bounds_and_parallel_probes(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    context = main.build_context(Settings(test_mode=True))
    db_entered, broker_entered = asyncio.Event(), asyncio.Event()

    async def database_probe(engine: object) -> bool:
        db_entered.set()
        await broker_entered.wait()
        return True

    async def broker_probe() -> bool:
        broker_entered.set()
        await db_entered.wait()
        if failure == "exception":
            raise OSError("connection lost")
        await asyncio.Event().wait()
        return False

    context.broker_probe = MagicMock(ping=broker_probe)
    monkeypatch.setattr(health, "check_database", database_probe)
    monkeypatch.setattr(health, "READINESS_TIMEOUT_SECONDS", 0.02)
    app = main.create_app()
    app.state.context = context
    try:
        async with (
            asyncio.timeout(1),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            result = await client.get("/health/ready")
        assert result.status_code == 503
        assert result.json() == {
            "status": "degraded",
            "db": "ok",
            "broker": "fail",
        }
    finally:
        assert context.people_client is not None
        assert context.broker_client is not None
        await context.people_client.aclose()
        await context.broker_client.aclose()
        await context.engine.dispose()
