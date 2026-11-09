"""Фоновые задачи: реестр и периодический запуск.

Соглашение пула (TASK.md, §3.6):

- при ``TEST_MODE=1`` периодический запуск отключён, задачи выполняются только через
  ``POST /__test/jobs/{job_name}/run`` и возвращают ``{"processed": <int>}``;
- при ``TEST_MODE=0`` все задачи запускаются с интервалом ``JOBS_INTERVAL_SECONDS``.

Каркас даёт реестр и цикл. Сами задачи (например, перевод занятий по времени) пишет автор сервиса
и регистрирует их в ``main.py``: ``registry.register("<job-name>", callable)``. Задача — любая
корутина без аргументов, возвращающая число обработанных записей; всё, что ей нужно (фабрика сессий,
``Clock``, издатель событий), передаётся замыканием или объектом с ``__call__``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

JobFn = Callable[[], Awaitable[int]]

log = logging.getLogger(__name__)


class UnknownJobError(LookupError):
    """Задача с таким именем не зарегистрирована."""


class JobRegistry:
    """Именованные фоновые задачи."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobFn] = {}

    def register(self, name: str, job: JobFn) -> None:
        """Зарегистрировать задачу; повторная регистрация имени — ошибка программиста."""
        if name in self._jobs:
            msg = f"Задача {name!r} уже зарегистрирована"
            raise ValueError(msg)
        self._jobs[name] = job

    def names(self) -> list[str]:
        """Имена задач в порядке регистрации."""
        return list(self._jobs)

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    async def run(self, name: str) -> int:
        """Выполнить задачу один раз и вернуть число обработанных записей."""
        try:
            job = self._jobs[name]
        except KeyError as exc:
            raise UnknownJobError(name) from exc
        return await job()


async def run_periodically(
    registry: JobRegistry, interval_seconds: float, stop: asyncio.Event
) -> None:
    """Запускать все задачи реестра по кругу, пока не взведён ``stop``.

    Ошибка одной задачи логируется и не останавливает цикл: следующая итерация повторит попытку.
    """
    while not stop.is_set():
        for name in registry.names():
            try:
                processed = await registry.run(name)
            except Exception:
                log.exception("Фоновая задача %s завершилась ошибкой", name)
            else:
                log.debug("Фоновая задача %s: processed=%d", name, processed)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
