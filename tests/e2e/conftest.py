"""Общие фикстуры e2e: клиенты, брокер, чистое состояние перед сценарием, отчёт.

Параметры окружения необязательны (см. .env.example и ``tests/e2e/env.py``): ``E2E_BASE_URL``,
``E2E_STUB_URL``, ``BROKER``, ``E2E_BROKER_URL``; без ``BROKER`` брокер определяется по порту.
Данные задания (сервис, фиксированное время, идентификаторы) — в ``tests/e2e/assignment.py``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest

from tests.e2e import assignment
from tests.e2e.broker import BrokerHelper, open_broker
from tests.e2e.contract import ContractChecker
from tests.e2e.env import E2ESettings, load_settings
from tests.e2e.report import Recorder, ReportCollector, ScenarioRecord, record_from_item

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "openapi.yaml"

REPORT_KEY: pytest.StashKey[ReportCollector] = pytest.StashKey()
RECORD_KEY: pytest.StashKey[ScenarioRecord] = pytest.StashKey()
CHECKER_KEY: pytest.StashKey[ContractChecker] = pytest.StashKey()


# --- отчёт -------------------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-report",
        action="store",
        default=None,
        help="Путь к JSON-отчёту e2e в формате скрытого набора",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[REPORT_KEY] = ReportCollector()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when == "call":
        record = item.stash.get(RECORD_KEY, None) or record_from_item(item)
        record.outcome = report.outcome
        if report.failed:
            record.message = str(report.longrepr)[-2000:]
        checker = item.stash.get(CHECKER_KEY, None)
        if checker is not None and checker.violations:
            record.contract_violation = True
        item.config.stash[REPORT_KEY].add(item.nodeid, record)
    return report


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = session.config.getoption("--e2e-report")
    if path:
        session.config.stash[REPORT_KEY].write(Path(str(path)))


# --- окружение ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_settings() -> E2ESettings:
    return load_settings(os.environ)


@pytest.fixture(scope="session")
def openapi_spec() -> ContractChecker:
    return ContractChecker.from_file(OPENAPI_PATH)


@pytest.fixture
def contract(request: pytest.FixtureRequest, openapi_spec: ContractChecker) -> ContractChecker:
    """Проверка схем для одного теста; нарушения попадают в отчёт как ``contract_violation``."""
    checker = openapi_spec.fork()
    request.node.stash[CHECKER_KEY] = checker
    return checker


@pytest.fixture
def record(request: pytest.FixtureRequest) -> Recorder:
    """Ожидаемое и фактическое поведение сценария для отчёта."""
    scenario = record_from_item(request.node)
    request.node.stash[RECORD_KEY] = scenario
    return Recorder(scenario)


@pytest.fixture
async def api(e2e_settings: E2ESettings) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент сервиса кандидата."""
    async with httpx.AsyncClient(base_url=e2e_settings.base_url, timeout=15.0) as client:
        yield client


@pytest.fixture
async def stub(e2e_settings: E2ESettings) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент заглушки (служебные эндпоинты ``/__stub/*``)."""
    async with httpx.AsyncClient(base_url=e2e_settings.stub_url, timeout=15.0) as client:
        yield client


@pytest.fixture
async def broker(e2e_settings: E2ESettings) -> AsyncIterator[BrokerHelper]:
    """Хелпер брокера, привязанный к событиям сервиса задания."""
    helper = await open_broker(e2e_settings.broker, e2e_settings.broker_url, assignment.SERVICE)
    try:
        yield helper
    finally:
        await helper.aclose()


@pytest.fixture(autouse=True)
async def clean_state(
    api: httpx.AsyncClient, stub: httpx.AsyncClient, broker: BrokerHelper
) -> None:
    """Общие предусловия каждого сценария (TASK.md, Приложение B)."""
    response = await api.put("/__test/time", json={"now": assignment.FROZEN_NOW})
    assert response.status_code == 200, f"PUT /__test/time: {response.status_code} {response.text}"
    response = await api.post("/__test/reset")
    assert response.status_code == 204, (
        f"POST /__test/reset: {response.status_code} {response.text}"
    )
    response = await stub.delete("/__stub/config")
    assert response.status_code == 200, f"DELETE /__stub/config: {response.status_code}"
    response = await stub.delete("/__stub/stats")
    assert response.status_code == 204, f"DELETE /__stub/stats: {response.status_code}"
    await broker.purge()


@pytest.fixture
def ids() -> dict[str, str]:
    """Идентификаторы seed и фикстур заглушки (сокращения из задания)."""
    return dict(assignment.IDS)
