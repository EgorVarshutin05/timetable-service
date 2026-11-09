"""E2E-02. Конфликт по преподавателю с seed-занятием L1 (FR-01, BR-05, FR-03)."""

import httpx
import pytest

from tests.e2e.contract import ContractChecker
from tests.e2e.report import Recorder


@pytest.mark.scenario(id="E2E-02", fr=["FR-01", "FR-03"], must=True)
async def test_instructor_conflict_is_rejected_and_nothing_is_created(
    api: httpx.AsyncClient, ids: dict[str, str], contract: ContractChecker, record: Recorder
) -> None:
    """Конфликт по преподавателю: 409 SCHEDULE_CONFLICT с пересечением; занятие не создано."""
    record.expected(
        "409 SCHEDULE_CONFLICT, details.conflicts=[{resource: instructor, lesson_id: L1, "
        "overlap: 2026-12-01T06:00:00Z–06:30:00Z}]; "
        "GET /lessons?instructor_id=I1&from=…&to=… → total=1"
    )
    body = {
        "group_id": ids["G3"],
        "instructor_id": ids["I1"],
        "classroom_id": None,
        "format": "ONLINE",
        "starts_at": "2026-12-01T06:00:00Z",
        "ends_at": "2026-12-01T07:00:00Z",
    }

    response = await api.post("/lessons", json=body)
    record.actual(f"POST /lessons → {response.status_code}: {response.text[:300]}")
    details = contract.assert_error(response, 409, "SCHEDULE_CONFLICT")

    assert details["conflicts"] == [
        {
            "resource": "instructor",
            "lesson_id": ids["L1"],
            "overlap": {"starts_at": "2026-12-01T06:00:00Z", "ends_at": "2026-12-01T06:30:00Z"},
        }
    ]

    listed = await api.get(
        "/lessons",
        params={
            "instructor_id": ids["I1"],
            "from": "2026-12-01T00:00:00Z",
            "to": "2026-12-02T00:00:00Z",
        },
    )
    record.actual(f"{record.record.actual}; GET /lessons → {listed.status_code}")
    assert listed.status_code == 200, listed.text
    page = listed.json()
    contract.assert_schema(page, "LessonPage")
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [ids["L1"]]
