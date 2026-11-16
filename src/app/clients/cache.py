"""Ограниченный TTL/LRU-кэш значений; I/O никогда не удерживает общий mutex."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import RLock
from uuid import UUID


@dataclass
class _Flight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class AsyncTTLCache[T]:
    """Один owner event loop, как у HTTPX AsyncClient; metadata защищены RLock.

    clear() синхронен и может вызываться из другого потока. Старый in-flight ответ
    может дойти до своего caller, но generation не даст ему заселить очищенный кэш.
    """

    def __init__(self, ttl: float, timer: Callable[[], float], maxsize: int = 256) -> None:
        if ttl < 0 or maxsize < 1:
            raise ValueError("TTL >= 0 и maxsize >= 1 обязательны")
        self._ttl, self._timer, self._maxsize = ttl, timer, maxsize
        self._values: OrderedDict[UUID, tuple[float, T]] = OrderedDict()
        self._flights: dict[UUID, _Flight] = {}
        self._mutex = RLock()
        self._generation = 0

    def clear(self) -> None:
        with self._mutex:
            self._generation += 1
            self._values.clear()

    def _lookup(self, key: UUID) -> tuple[T] | None:
        # Вызывается только под _mutex. Удаляем expired entries также для других keys.
        now = self._timer()
        for expired in [key for key, (deadline, _) in self._values.items() if deadline <= now]:
            del self._values[expired]
        entry = self._values.get(key)
        if entry is None:
            return None
        self._values.move_to_end(key)
        return (entry[1],)

    async def get(self, key: UUID, fetch: Callable[[], Awaitable[T]]) -> T:
        if self._ttl == 0:
            return await fetch()
        with self._mutex:
            cached = self._lookup(key)
            if cached is not None:
                return cached[0]
            flight = self._flights.setdefault(key, _Flight())
            flight.users += 1
        try:
            async with flight.lock:
                with self._mutex:
                    cached = self._lookup(key)
                    if cached is not None:
                        return cached[0]
                    generation = self._generation
                # Ошибки/отмена не кэшируются. Блокируется только тот же ключ.
                value = await fetch()
                with self._mutex:
                    if generation == self._generation:
                        self._values[key] = (self._timer() + self._ttl, value)
                        self._values.move_to_end(key)
                        while len(self._values) > self._maxsize:
                            self._values.popitem(last=False)
                return value
        finally:
            with self._mutex:
                flight.users -= 1
                if flight.users == 0:
                    del self._flights[key]
