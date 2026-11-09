"""Настройки приложения: все значения берутся из переменных окружения (см. .env.example)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BrokerKind = Literal["rabbitmq", "kafka"]


class Settings(BaseSettings):
    """Переменные окружения сервиса. Имена полей совпадают с переменными из .env.example."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    database_url: str = Field(
        default="postgresql+asyncpg://wa:wa@localhost:5432/wa",
        description="DSN PostgreSQL для SQLAlchemy async (драйвер asyncpg)",
    )
    broker: BrokerKind = Field(default="rabbitmq", description="Выбранный брокер: rabbitmq | kafka")
    broker_url: str = Field(
        default="amqp://wa:wa@localhost:5672/",
        description="URL брокера: amqp://... для RabbitMQ, host:port для Kafka",
    )
    stub_base_url: str = Field(
        default="http://localhost:8100", description="Базовый URL заглушки соседнего сервиса"
    )
    stub_timeout_ms: int = Field(default=2000, ge=1, description="Таймаут одного вызова заглушки")
    test_mode: bool = Field(
        default=False,
        description="TEST_MODE=1: доступны /__test/*, периодические задачи отключены",
    )
    seed_on_start: bool = Field(default=False, description="SEED_ON_START=1: seed при старте")
    jobs_interval_seconds: int = Field(
        default=30, ge=1, le=60, description="Интервал фоновых задач при TEST_MODE=0"
    )
    log_level: str = Field(default="INFO", description="Уровень логирования")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единственный экземпляр настроек на процесс (кэш сбрасывается в тестах через cache_clear)."""
    return Settings()
