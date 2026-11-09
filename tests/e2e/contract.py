"""Проверка ответов сервиса по схемам ``openapi.yaml`` (поле ``contract_violation`` отчёта).

Схемы OpenAPI 3.1 — это JSON Schema 2020-12, поэтому ``components.schemas.<Name>`` валидируются
напрямую библиотекой ``jsonschema``; ``$ref`` внутри документа разрешаются через ``referencing``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_URI = "openapi.yaml"


class ContractViolation(AssertionError):
    """Ответ не соответствует контракту."""


class ContractChecker:
    """Валидатор схем одного ``openapi.yaml``. Ведёт список нарушений для отчёта."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        resource = Resource.from_contents(spec, default_specification=DRAFT202012)
        self._registry: Registry[Any] = Registry().with_resource(SPEC_URI, resource)
        self.violations: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> ContractChecker:
        with path.open(encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        if not isinstance(spec, dict):
            msg = f"{path}: ожидается документ OpenAPI"
            raise TypeError(msg)
        return cls(spec)

    def fork(self) -> ContractChecker:
        """Новый валидатор над тем же документом с пустым списком нарушений (для одного теста)."""
        return ContractChecker(self._spec)

    def schema_names(self) -> list[str]:
        """Имена схем из ``components.schemas``."""
        components = self._spec.get("components", {})
        schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
        return sorted(schemas) if isinstance(schemas, dict) else []

    def errors(self, payload: Any, schema_name: str) -> list[str]:
        """Список сообщений об ошибках валидации ``payload`` против схемы ``schema_name``."""
        if schema_name not in self.schema_names():
            msg = f"Схема {schema_name!r} не найдена в {SPEC_URI}"
            raise KeyError(msg)
        validator = Draft202012Validator(
            {"$ref": f"{SPEC_URI}#/components/schemas/{schema_name}"},
            registry=self._registry,
        )
        messages: list[str] = []
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            messages.append(f"{schema_name}: {location}: {error.message}")
        return messages

    def assert_schema(self, payload: Any, schema_name: str) -> None:
        """Проверить ``payload`` по схеме; нарушение запоминается и поднимается как ошибка теста."""
        messages = self.errors(payload, schema_name)
        if messages:
            self.violations.extend(messages)
            raise ContractViolation("\n".join(messages))

    def assert_error(self, response: httpx.Response, status_code: int, code: str) -> dict[str, Any]:
        """Проверить ответ с ошибкой: статус, формат ``Error`` и код. Возвращает ``details``."""
        body = _json_body(response)
        self.assert_schema(body, "Error")
        if response.status_code != status_code or body["error"]["code"] != code:
            message = (
                f"ожидалось {status_code} {code}, получено "
                f"{response.status_code} {body['error']['code']}"
            )
            raise AssertionError(message)
        details = body["error"]["details"]
        if not isinstance(details, dict):
            raise ContractViolation("Error.details должен быть объектом")
        return details


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        msg = f"Ответ не JSON (HTTP {response.status_code}): {response.text[:200]!r}"
        raise ContractViolation(msg) from exc
