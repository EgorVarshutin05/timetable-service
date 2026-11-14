"""Перехватчик событий без сети; подставляется только тестами."""

from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.domain.events import LessonEvent

SPEC = yaml.safe_load((Path(__file__).resolve().parents[2] / "asyncapi.yaml").read_text())
REGISTRY: Registry[Any] = Registry().with_resource(
    "asyncapi.yaml", Resource.from_contents(SPEC, default_specification=DRAFT202012)
)


def assert_event(event: dict[str, Any]) -> None:
    validator = Draft202012Validator(
        {"$ref": f"asyncapi.yaml#/components/messages/{event['event_type']}/payload"},
        registry=REGISTRY,
        format_checker=FormatChecker(),
    )
    validator.validate(event)


class CapturingPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, event: LessonEvent) -> None:
        body = event.to_dict()
        assert_event(body)
        self.events.append(body)

    async def consume(
        self, timeout_seconds: float, *, max_messages: int | None = None
    ) -> list[dict[str, Any]]:
        events, self.events = self.events, []
        return events

    async def purge(self) -> None:
        self.events.clear()

    async def aclose(self) -> None:
        pass
