"""people-stub — заглушка сервиса преподавателей и аудиторий учебного центра «WA».

Контракт — ``stub-openapi.yaml``. Данные — ``fixtures.json`` в памяти, состояние между запросами
не хранится.
Режимы отказов задаются переменными окружения (начальное состояние) и меняются на лету через
``/__stub/config``; счётчики вызовов и ``max_in_flight`` — ``/__stub/stats``.

Порядок применения режимов к предметному запросу:

1. запрос учитывается в статистике (``in_flight`` растёт до ожидания — так виден параллелизм
   клиента);
2. ``down`` → пауза ``latency_ms`` и 503;
3. идентификатор в ``timeout_ids`` → пауза 10 с (вместо ``latency_ms``);
4. иначе пауза ``latency_ms``;
5. идентификатор в ``fail_ids`` → 500 ``STUB_FAILURE``;
6. флаг фикстуры (``fail_availability`` у преподавателя) → 500 на эндпоинте окон доступности;
7. обычный ответ: 200 или 404 ``NOT_FOUND``.

Служебные эндпоинты (``/health``, ``/__stub/*``) не задерживаются и в статистику не входят;
``/health`` отвечает 503 только в режиме ``down``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

FIXTURES_PATH = Path(os.environ.get("STUB_FIXTURES", Path(__file__).with_name("fixtures.json")))
TIMEOUT_DELAY_SECONDS = 10.0

PATH_INSTRUCTOR = "/instructors/{instructor_id}"
PATH_AVAILABILITY = "/instructors/{instructor_id}/availability"
PATH_CLASSROOM = "/classrooms/{classroom_id}"


# --- конфигурация режимов ----------------------------------------------------------------------


class StubConfig(BaseModel):
    """Режимы заглушки (``GET /__stub/config``)."""

    model_config = ConfigDict(extra="forbid")

    latency_ms: int = Field(default=300, ge=0)
    fail_ids: list[str] = Field(default_factory=list)
    timeout_ids: list[str] = Field(default_factory=list)
    down: bool = False


class StubConfigPatch(BaseModel):
    """Частичное изменение режимов (``PUT /__stub/config``)."""

    model_config = ConfigDict(extra="forbid")

    latency_ms: int | None = Field(default=None, ge=0)
    fail_ids: list[str] | None = None
    timeout_ids: list[str] | None = None
    down: bool | None = None


def _split_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def config_from_env(env: dict[str, str] | None = None) -> StubConfig:
    """Начальные режимы из переменных окружения ``STUB_*``."""
    source = os.environ if env is None else env
    return StubConfig(
        latency_ms=int(source.get("STUB_LATENCY_MS", "300")),
        fail_ids=_split_ids(source.get("STUB_FAIL_IDS", "")),
        timeout_ids=_split_ids(source.get("STUB_TIMEOUT_IDS", "")),
        down=source.get("STUB_DOWN", "0") == "1",
    )


# --- статистика ----------------------------------------------------------------------------------


@dataclass
class Stats:
    """Счётчики предметных запросов с момента последнего сброса."""

    total: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    by_path: dict[str, int] = field(default_factory=dict)
    by_id: dict[str, int] = field(default_factory=dict)

    def enter(self, path: str, entity_id: str) -> None:
        self.total += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.by_path[path] = self.by_path.get(path, 0) + 1
        self.by_id[entity_id] = self.by_id.get(entity_id, 0) + 1

    def leave(self) -> None:
        self.in_flight -= 1

    def reset(self) -> None:
        """Обнулить счётчики; текущие запросы в обработке остаются учтёнными в ``in_flight``."""
        self.total = 0
        self.max_in_flight = self.in_flight
        self.by_path.clear()
        self.by_id.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "in_flight": self.in_flight,
            "max_in_flight": self.max_in_flight,
            "by_path": dict(self.by_path),
            "by_id": dict(self.by_id),
        }


# --- фикстуры ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Instructor:
    id: str
    full_name: str
    max_hours_per_day: float
    timezone: str
    windows: list[dict[str, Any]]
    fail_availability: bool = False

    def profile(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "max_hours_per_day": self.max_hours_per_day,
            "timezone": self.timezone,
        }

    def availability(self) -> dict[str, Any]:
        return {"instructor_id": self.id, "timezone": self.timezone, "windows": list(self.windows)}


@dataclass(frozen=True)
class Classroom:
    id: str
    name: str
    capacity: int
    online_capable: bool

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "capacity": self.capacity,
            "online_capable": self.online_capable,
        }


def load_fixtures(path: Path) -> tuple[dict[str, Instructor], dict[str, Classroom]]:
    """Прочитать ``fixtures.json``."""
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    instructors = {
        item["id"]: Instructor(
            id=item["id"],
            full_name=item["full_name"],
            max_hours_per_day=item["max_hours_per_day"],
            timezone=item["timezone"],
            windows=list(item.get("windows", [])),
            fail_availability=bool(item.get("fail_availability", False)),
        )
        for item in raw["instructors"]
    }
    classrooms = {
        item["id"]: Classroom(
            id=item["id"],
            name=item["name"],
            capacity=item["capacity"],
            online_capable=item["online_capable"],
        )
        for item in raw["classrooms"]
    }
    return instructors, classrooms


# --- состояние процесса --------------------------------------------------------------------------


class StubState:
    """Режимы, статистика и фикстуры одного процесса заглушки."""

    def __init__(self, fixtures_path: Path, env: dict[str, str] | None = None) -> None:
        self.env_config = config_from_env(env)
        self.config = self.env_config.model_copy(deep=True)
        self.stats = Stats()
        self.instructors, self.classrooms = load_fixtures(fixtures_path)

    def reset_config(self) -> None:
        self.config = self.env_config.model_copy(deep=True)

    def apply_patch(self, patch: StubConfigPatch) -> None:
        self.config = self.config.model_copy(update=patch.model_dump(exclude_none=True))

    @asynccontextmanager
    async def track(self, path: str, entity_id: str) -> AsyncIterator[None]:
        """Учесть предметный запрос в статистике на всё время его обработки."""
        self.stats.enter(path, entity_id)
        try:
            yield
        finally:
            self.stats.leave()

    async def simulate_failures(self, entity_id: str) -> JSONResponse | None:
        """Выдержать задержку и вернуть ответ-отказ, если режимы этого требуют; иначе ``None``."""
        config = self.config
        if config.down:
            await asyncio.sleep(config.latency_ms / 1000)
            return error(503, "STUB_DOWN", "Service unavailable")
        if entity_id in config.timeout_ids:
            await asyncio.sleep(TIMEOUT_DELAY_SECONDS)
        else:
            await asyncio.sleep(config.latency_ms / 1000)
        if entity_id in config.fail_ids:
            return error(500, "STUB_FAILURE", "Injected failure")
        return None


def error(status_code: int, code: str, message: str) -> JSONResponse:
    """Ответ с ошибкой в формате платформы."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
    )


# --- приложение ----------------------------------------------------------------------------------


def create_app(fixtures_path: Path = FIXTURES_PATH, env: dict[str, str] | None = None) -> FastAPI:
    """Собрать приложение заглушки; ``env`` задаёт режимы в тестах вместо переменных окружения."""
    state = StubState(fixtures_path, env)
    app = FastAPI(title="people-stub", version="0.1.0")
    app.state.stub = state

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = {
            ".".join(str(p) for p in err.get("loc", ())): err.get("msg", "") for err in exc.errors()
        }
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid values",
                    "details": fields,
                }
            },
        )

    @app.get(PATH_INSTRUCTOR)
    async def get_instructor(instructor_id: str) -> Response:
        async with state.track(PATH_INSTRUCTOR, instructor_id):
            failure = await state.simulate_failures(instructor_id)
            if failure is not None:
                return failure
            instructor = state.instructors.get(instructor_id)
            if instructor is None:
                return error(404, "NOT_FOUND", "Not found")
            return JSONResponse(instructor.profile())

    @app.get(PATH_AVAILABILITY)
    async def get_availability(instructor_id: str) -> Response:
        async with state.track(PATH_AVAILABILITY, instructor_id):
            failure = await state.simulate_failures(instructor_id)
            if failure is not None:
                return failure
            instructor = state.instructors.get(instructor_id)
            if instructor is None:
                return error(404, "NOT_FOUND", "Not found")
            if instructor.fail_availability:
                return error(500, "STUB_FAILURE", "Injected failure")
            return JSONResponse(instructor.availability())

    @app.get(PATH_CLASSROOM)
    async def get_classroom(classroom_id: str) -> Response:
        async with state.track(PATH_CLASSROOM, classroom_id):
            failure = await state.simulate_failures(classroom_id)
            if failure is not None:
                return failure
            classroom = state.classrooms.get(classroom_id)
            if classroom is None:
                return error(404, "NOT_FOUND", "Not found")
            return JSONResponse(classroom.public())

    @app.get("/health")
    async def health() -> Response:
        if state.config.down:
            return error(503, "STUB_DOWN", "Service unavailable")
        return JSONResponse({"status": "ok"})

    @app.get("/__stub/config")
    async def get_config() -> StubConfig:
        return state.config

    @app.put("/__stub/config")
    async def put_config(patch: StubConfigPatch) -> StubConfig:
        state.apply_patch(patch)
        return state.config

    @app.delete("/__stub/config")
    async def reset_config() -> StubConfig:
        state.reset_config()
        return state.config

    @app.get("/__stub/stats")
    async def get_stats() -> dict[str, Any]:
        return state.stats.snapshot()

    @app.delete("/__stub/stats", status_code=204, response_class=Response)
    async def reset_stats() -> Response:
        state.stats.reset()
        return Response(status_code=204)

    return app


app = create_app()
