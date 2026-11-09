"""E2E-хелпер брокера: публикация, чтение и очистка событий в RabbitMQ и Kafka.

Выбор реализации и адрес — из ``tests/e2e/env.py`` (``BROKER`` или автоопределение по порту,
``E2E_BROKER_URL``). Соглашения пула (TASK.md, §3.4):

- RabbitMQ: topic exchange ``wa.events``, routing key ``<service>.<EventType>``;
- Kafka: топик ``wa.<service>.events``, ключ сообщения — идентификатор агрегата.

Хелпер читает события выбранного сервиса (``consume``), публикует события от имени любого сервиса
(``publish`` — для заданий, где сервис кандидата потребляет чужие события) и очищает накопленное
перед сценарием (``purge``). Используется и публичными, и скрытыми e2e.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue, AbstractRobustConnection
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from tests.e2e.env import BrokerKind

Event = dict[str, Any]

EXCHANGE = "wa.events"
POLL_INTERVAL = 0.1


class BrokerHelper(Protocol):
    """Единый интерфейс для обеих реализаций."""

    async def publish(self, source: str, event: Event, *, key: str | None = None) -> None:
        """Опубликовать конверт события от имени сервиса ``source``."""
        ...

    async def consume(
        self, timeout_seconds: float, *, max_messages: int | None = None
    ) -> list[Event]:
        """Собрать события сервиса за ``timeout_seconds`` (или раньше, набрав ``max_messages``)."""
        ...

    async def purge(self) -> None:
        """Удалить всё, что накопилось до начала сценария."""
        ...

    async def aclose(self) -> None:
        """Закрыть соединения."""
        ...


def _encode(event: Event) -> bytes:
    return json.dumps(event, ensure_ascii=False).encode("utf-8")


def _decode(body: bytes) -> Event:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        msg = "Событие должно быть JSON-объектом"
        raise TypeError(msg)
    return payload


class RabbitHelper:
    """RabbitMQ: эксклюзивная очередь на ``<service>.#``, живёт пока открыто соединение."""

    def __init__(self, url: str, service: str) -> None:
        self._url = url
        self._service = service
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None
        self._queue: AbstractQueue | None = None

    async def open(self) -> RabbitHelper:
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        self._queue = await self._channel.declare_queue(
            f"e2e.{self._service}", exclusive=True, auto_delete=True
        )
        await self._queue.bind(self._exchange, routing_key=f"{self._service}.#")
        return self

    def _ready(self) -> tuple[AbstractExchange, AbstractQueue]:
        if self._exchange is None or self._queue is None:
            msg = "RabbitHelper не открыт: вызовите open()"
            raise RuntimeError(msg)
        return self._exchange, self._queue

    async def publish(self, source: str, event: Event, *, key: str | None = None) -> None:
        exchange, _ = self._ready()
        message = aio_pika.Message(
            body=_encode(event),
            content_type="application/json",
            message_id=str(event.get("event_id", "")),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await exchange.publish(message, routing_key=f"{source}.{event['event_type']}")

    async def consume(
        self, timeout_seconds: float, *, max_messages: int | None = None
    ) -> list[Event]:
        _, queue = self._ready()
        deadline = time.monotonic() + timeout_seconds
        events: list[Event] = []
        while time.monotonic() < deadline:
            incoming = await queue.get(no_ack=True, fail=False)
            if incoming is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            events.append(_decode(incoming.body))
            if max_messages is not None and len(events) >= max_messages:
                break
        return events

    async def purge(self) -> None:
        _, queue = self._ready()
        await queue.purge()

    async def aclose(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None


class KafkaHelper:
    """Kafka: потребитель без группы читает ``wa.<service>.events`` с позиции на момент purge."""

    def __init__(self, bootstrap: str, service: str) -> None:
        self._bootstrap = bootstrap
        self._service = service
        self._topic = f"wa.{service}.events"
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._partitions: list[TopicPartition] = []

    async def _ensure_topic(self, topic: str) -> None:
        admin = AIOKafkaAdminClient(bootstrap_servers=self._bootstrap)
        await admin.start()
        try:
            await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
        except TopicAlreadyExistsError:
            pass
        finally:
            await admin.close()

    async def open(self) -> KafkaHelper:
        await self._ensure_topic(self._topic)
        self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap)
        await self._producer.start()
        self._consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap,
            enable_auto_commit=False,
            auto_offset_reset="latest",
        )
        await self._consumer.start()
        partitions = self._consumer.partitions_for_topic(self._topic) or {0}
        self._partitions = [TopicPartition(self._topic, p) for p in sorted(partitions)]
        self._consumer.assign(self._partitions)
        await self._consumer.seek_to_end(*self._partitions)
        return self

    def _ready(self) -> tuple[AIOKafkaConsumer, AIOKafkaProducer]:
        if self._consumer is None or self._producer is None:
            msg = "KafkaHelper не открыт: вызовите open()"
            raise RuntimeError(msg)
        return self._consumer, self._producer

    async def publish(self, source: str, event: Event, *, key: str | None = None) -> None:
        _, producer = self._ready()
        topic = f"wa.{source}.events"
        if topic != self._topic:
            await self._ensure_topic(topic)
        await producer.send_and_wait(
            topic,
            value=_encode(event),
            key=key.encode("utf-8") if key else None,
            headers=[("event_type", str(event["event_type"]).encode("utf-8"))],
        )

    async def consume(
        self, timeout_seconds: float, *, max_messages: int | None = None
    ) -> list[Event]:
        consumer, _ = self._ready()
        deadline = time.monotonic() + timeout_seconds
        events: list[Event] = []
        while time.monotonic() < deadline:
            remaining_ms = max(int((deadline - time.monotonic()) * 1000), 1)
            batches = await consumer.getmany(timeout_ms=min(remaining_ms, 500))
            for records in batches.values():
                for record in records:
                    events.append(_decode(record.value))
            if max_messages is not None and len(events) >= max_messages:
                break
        return events

    async def purge(self) -> None:
        consumer, _ = self._ready()
        await consumer.seek_to_end(*self._partitions)
        # seek_to_end ленив: позиция фиксируется первым чтением
        for partition in self._partitions:
            await consumer.position(partition)

    async def aclose(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


async def open_broker(kind: BrokerKind, url: str, service: str) -> BrokerHelper:
    """Открыть хелпер выбранного брокера для сервиса ``service``."""
    if kind == "rabbitmq":
        return await RabbitHelper(url, service).open()
    return await KafkaHelper(url, service).open()
