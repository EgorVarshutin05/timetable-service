"""Точка входа приложения: сборка FastAPI, lifespan, подключение роутеров.

Поднимает процесс с БД, часами, seed, чтением ресурсов и служебными эндпоинтами.
Клиент people-stub закрывается вместе с ресурсами процесса.
Зарегистрирован sweeper, брокер подключается лениво и закрывается вместе с процессом.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api import groups, health, instructors, lessons, series, testing
from app.broker import make_broker
from app.clients.people import PeopleClient
from app.clock import AdjustableClock
from app.context import AppContext
from app.db import make_engine, make_session_factory
from app.errors import install_error_handlers
from app.jobs import JobRegistry, run_periodically
from app.seed import load_seed, read_all, reset_data
from app.services.lifecycle import sweep_lessons
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)


def build_context(settings: Settings) -> AppContext:
    """Создать зависимости процесса (вне lifespan, чтобы тесты собирали контекст без сети)."""
    engine = make_engine(settings.database_url)
    broker = make_broker(settings)
    context = AppContext(
        settings=settings,
        engine=engine,
        session_factory=make_session_factory(engine),
        clock=AdjustableClock(),
        broker_probe=broker,
        broker_client=broker,
        event_publisher=broker,
        jobs=JobRegistry(),
        people_client=PeopleClient(settings),
    )

    async def lesson_status_sweeper() -> int:
        return await sweep_lessons(context.session_factory, context.clock)

    context.jobs.register("lesson-status-sweeper", lesson_status_sweeper)

    async def reset_state() -> None:
        await reset_data(context.session_factory)

    context.reset_state = reset_state
    return context


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Жизненный цикл процесса: ресурсы создаются при старте и закрываются при остановке."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    context = build_context(settings)
    app.state.context = context

    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(context.engine.dispose)
        if context.broker_client is not None:
            cleanup.push_async_callback(context.broker_client.aclose)
        if context.people_client is not None:
            cleanup.push_async_callback(context.people_client.aclose)
        stop = asyncio.Event()
        periodic: asyncio.Task[None] | None = None
        try:
            if settings.seed_on_start:
                data = await asyncio.to_thread(read_all)
                async with context.session_factory() as session:
                    await load_seed(session, data)
                    await session.commit()
            if settings.test_mode:
                log.info("TEST_MODE=1: /__test/* включены, периодические задачи отключены")
            else:
                periodic = asyncio.create_task(
                    run_periodically(context.jobs, settings.jobs_interval_seconds, stop),
                    name="periodic-jobs",
                )
            yield
        finally:
            stop.set()
            if periodic is not None:
                await periodic


def create_app() -> FastAPI:
    """Собрать приложение."""
    app = FastAPI(
        title="WA service",
        version="0.1.0",
        lifespan=lifespan,
        # Нормативный контракт — openapi.yaml в корне репозитория; автогенерируемая схема
        # остаётся для удобства разработки.
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(testing.router)
    app.include_router(groups.router)
    app.include_router(lessons.router)
    app.include_router(series.router)
    app.include_router(instructors.router)
    # ---> точка расширения: app.include_router(<предметный роутер>)
    return app


app = create_app()
