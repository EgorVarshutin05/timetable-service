"""Отчёт e2e в формате скрытого набора: ``id``, ``scenario``, ``must``, ``expected``, ``actual``,
``contract_violation`` (плюс ``fr``, ``outcome`` и ``message`` для чтения человеком).

Подключается хуками из ``tests/e2e/conftest.py``; сценарий размечается маркером
``@pytest.mark.scenario(id="E2E-01", fr=["FR-01"], must=True)``, а ожидаемое и фактическое поведение
тест записывает через фикстуру ``record``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class ScenarioRecord:
    """Одна строка отчёта."""

    id: str
    scenario: str
    must: bool
    fr: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    outcome: str = "not run"
    contract_violation: bool = False
    message: str = ""


class Recorder:
    """То, что тест сообщает о себе: ожидаемое и фактическое поведение."""

    def __init__(self, record: ScenarioRecord) -> None:
        self.record = record

    def expected(self, text: str) -> None:
        self.record.expected = text

    def actual(self, text: str) -> None:
        self.record.actual = text


def record_from_item(item: pytest.Item) -> ScenarioRecord:
    """Собрать запись из маркера ``scenario`` (или из имени теста, если маркера нет)."""
    marker = item.get_closest_marker("scenario")
    scenario_id = str(marker.kwargs.get("id", item.name)) if marker else item.name
    fr = marker.kwargs.get("fr", []) if marker else []
    must = bool(marker.kwargs.get("must", True)) if marker else True
    doc = (item.obj.__doc__ or "").strip() if hasattr(item, "obj") else ""
    scenario = doc.splitlines()[0] if doc else item.name
    return ScenarioRecord(
        id=scenario_id,
        scenario=scenario,
        must=must,
        fr=[str(code) for code in fr],
    )


class ReportCollector:
    """Накапливает записи за сессию и пишет JSON."""

    def __init__(self) -> None:
        self.records: dict[str, ScenarioRecord] = {}

    def add(self, nodeid: str, record: ScenarioRecord) -> None:
        self.records[nodeid] = record

    def rows(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self.records.values()]

    def write(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.rows(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
