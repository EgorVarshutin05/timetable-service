"""FR-12: параллельное чтение people-stub и перевод ошибок в формат платформы."""

import asyncio
from collections.abc import Callable
from datetime import time
from decimal import Decimal
from time import monotonic
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.clients.cache import AsyncTTLCache
from app.domain.lesson import LessonFormat
from app.domain.people import (
    AvailabilityWindow,
    Classroom,
    InstructorAvailability,
    InstructorProfile,
    LessonPeople,
)
from app.errors import ApiError
from app.settings import Settings

Entity = Literal["instructor", "classroom"]
LocalTime = Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class _WithTimezone(BaseModel):
    timezone: str

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Ожидается IANA timezone") from exc
        return value


class _Instructor(_WithTimezone):
    id: UUID
    full_name: str
    max_hours_per_day: Decimal = Field(gt=0, allow_inf_nan=False)


class _Window(BaseModel):
    weekday: int = Field(ge=1, le=7, strict=True)
    starts_at: LocalTime = Field(validation_alias="from")
    ends_at: LocalTime = Field(validation_alias="to")

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("Окно должно заканчиваться позже начала в пределах дня")
        return self


class _Availability(_WithTimezone):
    instructor_id: UUID
    windows: list[_Window]


class _Classroom(BaseModel):
    id: UUID
    name: str
    capacity: int = Field(ge=1, strict=True)
    online_capable: bool = Field(strict=True)


def _unwrap[T](result: T | BaseException) -> T:
    if isinstance(result, BaseException):
        raise result
    return result


class PeopleClient:
    """Владеет одним HTTPX-клиентом на lifespan; transport подменяется в тестах."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_timer: Callable[[], float] = monotonic,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.stub_base_url,
            timeout=settings.stub_timeout_ms / 1000,
            transport=transport,
            trust_env=False,
        )
        ttl = settings.people_cache_ttl_seconds
        self._profiles = AsyncTTLCache[InstructorProfile](ttl, cache_timer)
        self._availability = AsyncTTLCache[InstructorAvailability](ttl, cache_timer)
        self._classrooms = AsyncTTLCache[Classroom](ttl, cache_timer)

    def clear_cache(self) -> None:
        """Инвалидация всех типов; старые загрузки не заполнят новый generation."""
        self._profiles.clear()
        self._availability.clear()
        self._classrooms.clear()

    @property
    def is_closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        self.clear_cache()
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @staticmethod
    def _unavailable(entity: Entity, entity_id: UUID) -> ApiError:
        return ApiError(
            502,
            "UPSTREAM_UNAVAILABLE",
            "Сервис преподавателей и аудиторий недоступен или вернул некорректный ответ",
            {"entity": entity, "id": str(entity_id)},
        )

    async def _fetch[T: BaseModel](
        self, path: str, entity: Entity, entity_id: UUID, schema: type[T]
    ) -> T:
        try:
            response = await self._http.get(path)
        except httpx.TimeoutException as exc:
            raise ApiError(
                504,
                "UPSTREAM_TIMEOUT",
                "Превышено время ожидания people-stub",
                {"entity": entity, "id": str(entity_id)},
            ) from exc
        except httpx.RequestError as exc:
            raise self._unavailable(entity, entity_id) from exc
        if response.status_code == 404:
            code = "INSTRUCTOR_NOT_FOUND" if entity == "instructor" else "CLASSROOM_NOT_FOUND"
            raise ApiError(
                422,
                code,
                "Сущность отсутствует в people-stub",
                {"entity": entity, "id": str(entity_id)},
            )
        if response.status_code != 200:
            raise self._unavailable(entity, entity_id)
        try:
            return schema.model_validate(response.json())
        except ValueError as exc:
            raise self._unavailable(entity, entity_id) from exc

    async def get_instructor(self, instructor_id: UUID) -> InstructorProfile:
        if self.is_closed:
            raise RuntimeError("PeopleClient закрыт")
        return await self._profiles.get(instructor_id, lambda: self._load_instructor(instructor_id))

    async def _load_instructor(self, instructor_id: UUID) -> InstructorProfile:
        item = await self._fetch(
            f"instructors/{instructor_id}", "instructor", instructor_id, _Instructor
        )
        if item.id != instructor_id:
            raise self._unavailable("instructor", instructor_id)
        return InstructorProfile(item.id, item.full_name, item.max_hours_per_day, item.timezone)

    async def get_availability(self, instructor_id: UUID) -> InstructorAvailability:
        if self.is_closed:
            raise RuntimeError("PeopleClient закрыт")
        return await self._availability.get(
            instructor_id, lambda: self._load_availability(instructor_id)
        )

    async def _load_availability(self, instructor_id: UUID) -> InstructorAvailability:
        item = await self._fetch(
            f"instructors/{instructor_id}/availability", "instructor", instructor_id, _Availability
        )
        if item.instructor_id != instructor_id:
            raise self._unavailable("instructor", instructor_id)
        windows = tuple(
            AvailabilityWindow(
                window.weekday,
                time.fromisoformat(window.starts_at),
                time.fromisoformat(window.ends_at),
            )
            for window in item.windows
        )
        return InstructorAvailability(item.instructor_id, item.timezone, windows)

    async def get_classroom(self, classroom_id: UUID) -> Classroom:
        if self.is_closed:
            raise RuntimeError("PeopleClient закрыт")
        return await self._classrooms.get(classroom_id, lambda: self._load_classroom(classroom_id))

    async def _load_classroom(self, classroom_id: UUID) -> Classroom:
        item = await self._fetch(
            f"classrooms/{classroom_id}", "classroom", classroom_id, _Classroom
        )
        if item.id != classroom_id:
            raise self._unavailable("classroom", classroom_id)
        return Classroom(item.id, item.name, item.capacity, item.online_capable)

    async def get_lesson_people(
        self,
        *,
        instructor_id: UUID,
        lesson_format: LessonFormat,
        classroom_id: UUID | None = None,
    ) -> LessonPeople:
        """Вызвать после BR-02; ошибки выбираются в порядке профиль → окна → аудитория.

        return_exceptions=True дожидается всех вызовов, исключая выбор по скорости ответа
        и продолжение запросов в фоне после ApiError. Отмена вызывающей задачи передаётся gather.
        """
        if lesson_format == LessonFormat.ONLINE:
            if classroom_id is not None:
                raise ValueError("Перед вызовом клиента необходимо проверить BR-02")
            profile, availability = await asyncio.gather(
                self.get_instructor(instructor_id),
                self.get_availability(instructor_id),
                return_exceptions=True,
            )
            return LessonPeople(_unwrap(profile), _unwrap(availability), None)
        if classroom_id is None:
            raise ValueError("Перед вызовом клиента необходимо проверить BR-02")
        profile, availability, classroom = await asyncio.gather(
            self.get_instructor(instructor_id),
            self.get_availability(instructor_id),
            self.get_classroom(classroom_id),
            return_exceptions=True,
        )
        return LessonPeople(_unwrap(profile), _unwrap(availability), _unwrap(classroom))
