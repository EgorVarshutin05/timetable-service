"""Адреса стека для e2e с точки зрения хоста и выбор брокера.

Переменные (необязательны; см. .env.example): ``E2E_BASE_URL``, ``E2E_STUB_URL``, ``BROKER``,
``E2E_BROKER_URL``. Если ``BROKER`` не задан, брокер определяется по открытому порту на localhost:
5672 — RabbitMQ, 9092 — Kafka. Так одни и те же тесты работают без настройки у кандидата
(брокер выбран в docker-compose.yml) и у проверяющих.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

BrokerKind = Literal["rabbitmq", "kafka"]
PortProbe = Callable[[str, int], bool]

BROKER_PORTS: dict[BrokerKind, int] = {"rabbitmq": 5672, "kafka": 9092}
DEFAULT_BROKER_URLS: dict[BrokerKind, str] = {
    "rabbitmq": "amqp://wa:wa@localhost:5672/",
    "kafka": "localhost:9092",
}


@dataclass(frozen=True)
class E2ESettings:
    """Адреса стека с точки зрения хоста."""

    base_url: str
    stub_url: str
    broker: BrokerKind
    broker_url: str


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Принимает ли ``host:port`` TCP-соединения."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_broker(value: str) -> BrokerKind:
    """Проверить значение ``BROKER``."""
    if value == "rabbitmq":
        return "rabbitmq"
    if value == "kafka":
        return "kafka"
    msg = f"BROKER должен быть rabbitmq или kafka, получено {value!r}"
    raise ValueError(msg)


def detect_broker(environ: Mapping[str, str], probe: PortProbe = port_open) -> BrokerKind:
    """``BROKER`` из окружения, иначе — единственный брокер с открытым портом на localhost."""
    explicit = environ.get("BROKER", "").strip()
    if explicit:
        return parse_broker(explicit)
    available = [kind for kind, port in BROKER_PORTS.items() if probe("localhost", port)]
    if len(available) == 1:
        return available[0]
    if not available:
        msg = (
            "Брокер не найден: порты 5672 (RabbitMQ) и 9092 (Kafka) на localhost закрыты. "
            "Поднимите стек (just up) или задайте BROKER и E2E_BROKER_URL."
        )
    else:
        msg = "Подняты оба брокера: задайте BROKER=rabbitmq или BROKER=kafka."
    raise RuntimeError(msg)


def load_settings(environ: Mapping[str, str], probe: PortProbe = port_open) -> E2ESettings:
    """Собрать настройки e2e из окружения."""
    broker = detect_broker(environ, probe)
    return E2ESettings(
        base_url=environ.get("E2E_BASE_URL", "http://localhost:8000"),
        stub_url=environ.get("E2E_STUB_URL", "http://localhost:8100"),
        broker=broker,
        broker_url=environ.get("E2E_BROKER_URL", DEFAULT_BROKER_URLS[broker]),
    )
