"""Ленивые подключения, реальные readiness RPC и подтверждённая отправка."""

import asyncio
import json
import logging
from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient

from app.domain.events import LessonEvent
from app.settings import Settings

log = logging.getLogger(__name__)


class BrokerProbe(Protocol):
    async def ping(self) -> bool: ...


class EventPublisher(Protocol):
    async def publish(self, event: LessonEvent) -> None: ...


class BrokerClient(BrokerProbe, EventPublisher, Protocol):
    async def aclose(self) -> None: ...


class UnconfiguredBrokerProbe:
    """Оставлен для вручную собранных изолированных контекстов каркаса."""

    async def ping(self) -> bool:
        return False


def _encode(event: LessonEvent) -> bytes:
    return json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8")


class RabbitBroker:
    def __init__(self, settings: Settings) -> None:
        self._url = settings.rabbitmq_url
        self._name = settings.rabbitmq_exchange
        self._timeout = settings.broker_timeout_ms / 1000
        self._lock = asyncio.Lock()
        self._closed = False
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def _disconnect(self) -> None:
        connection = self._connection
        self._connection = self._channel = self._exchange = None
        if connection is not None:
            await connection.close()

    async def _ready(self) -> AbstractExchange:
        if self._closed:
            raise RuntimeError("Брокер закрыт")
        if (
            self._connection is not None
            and not self._connection.is_closed
            and self._channel is not None
            and not self._channel.is_closed
            and self._exchange is not None
        ):
            return self._exchange
        await self._disconnect()
        self._connection = await aio_pika.connect(self._url, timeout=self._timeout)
        self._channel = await self._connection.channel(publisher_confirms=True)
        self._exchange = await self._channel.declare_exchange(
            self._name, aio_pika.ExchangeType.TOPIC, durable=True, timeout=self._timeout
        )
        return self._exchange

    async def publish(self, event: LessonEvent) -> None:
        async with self._lock:
            try:
                async with asyncio.timeout(self._timeout):
                    exchange = await self._ready()
                    await exchange.publish(
                        aio_pika.Message(
                            body=_encode(event),
                            content_type="application/json",
                            message_id=str(event.event_id),
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        ),
                        routing_key=f"timetable.{event.event_type}",
                        mandatory=False,
                        timeout=self._timeout,
                    )
            except BaseException:
                await self._disconnect()
                raise

    async def ping(self) -> bool:
        try:
            async with self._lock:
                try:
                    async with asyncio.timeout(self._timeout):
                        await self._ready()
                        assert self._channel is not None
                        # Passive declare — настоящий RPC, а не локальный is_closed.
                        await self._channel.declare_exchange(
                            self._name,
                            aio_pika.ExchangeType.TOPIC,
                            passive=True,
                            durable=True,
                            timeout=self._timeout,
                        )
                except BaseException:
                    await self._disconnect()
                    raise
            return True
        except Exception:
            log.debug("RabbitMQ readiness failed", exc_info=True)
            return False

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._disconnect()


class KafkaBroker:
    def __init__(self, settings: Settings) -> None:
        self._url = settings.broker_url
        self._topic = settings.kafka_topic
        self._timeout = settings.broker_timeout_ms / 1000
        self._timeout_ms = settings.broker_timeout_ms
        self._lock = asyncio.Lock()
        self._closed = False
        self._producer: AIOKafkaProducer | None = None

    async def _disconnect(self) -> None:
        producer, self._producer = self._producer, None
        if producer is not None:
            await producer.stop()

    async def publish(self, event: LessonEvent) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Брокер закрыт")
            try:
                async with asyncio.timeout(self._timeout):
                    if self._producer is None:
                        self._producer = AIOKafkaProducer(
                            bootstrap_servers=self._url,
                            acks="all",
                            request_timeout_ms=self._timeout_ms,
                        )
                        await self._producer.start()
                    await self._producer.send_and_wait(
                        self._topic,
                        value=_encode(event),
                        key=str(event.lesson_id).encode(),
                        headers=[("event_type", event.event_type.encode())],
                    )
            except BaseException:
                await self._disconnect()
                raise

    async def ping(self) -> bool:
        if self._closed:
            return False
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._url,
            request_timeout_ms=self._timeout_ms,
        )
        try:
            async with asyncio.timeout(self._timeout):
                await admin.start()
                await admin.describe_cluster()
            return True
        except Exception:
            log.debug("Kafka readiness failed", exc_info=True)
            return False
        finally:
            await admin.close()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._disconnect()


def make_broker(settings: Settings) -> BrokerClient:
    return RabbitBroker(settings) if settings.broker == "rabbitmq" else KafkaBroker(settings)
