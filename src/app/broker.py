"""Точка расширения для брокера.

Каркас не знает, какой брокер выбран и какая библиотека его обслуживает, поэтому здесь только
протокол проверки доступности для ``/health/ready`` и заглушка, которая честно отвечает
«не настроен».
Реализацию (подключение, публикация событий по asyncapi.yaml, пинг для readiness) добавляет автор
сервиса, а ``main.py`` подставляет её вместо ``UnconfiguredBrokerProbe``.
"""

from typing import Protocol


class BrokerProbe(Protocol):
    """Проверка доступности брокера для readiness."""

    async def ping(self) -> bool:
        """True, если брокер отвечает."""
        ...


class UnconfiguredBrokerProbe:
    """Брокер ещё не подключён: readiness сообщает ``broker: fail``."""

    async def ping(self) -> bool:
        """Всегда False — до тех пор, пока не подключён реальный брокер."""
        return False
