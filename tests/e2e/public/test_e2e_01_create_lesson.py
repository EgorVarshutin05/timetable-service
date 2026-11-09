"""E2E-01. Создание очного занятия без нарушений (FR-01, FR-04)."""

import httpx
import pytest

from tests.e2e.contract import ContractChecker
from tests.e2e.report import Recorder


@pytest.mark.scenario(id="E2E-01", fr=["FR-01", "FR-04"], must=True)
async def test_create_in_person_lesson_without_violations(
    api: httpx.AsyncClient, ids: dict[str, str], contract: ContractChecker, record: Recorder
) -> None:
    """Создание очного занятия без нарушений: 201, PLANNED, без предупреждений; GET — то же."""
    record.expected(
        "201 {lesson, warnings}; lesson.status=PLANNED, rescheduled_from_id=null, warnings=[]; "
        "GET /lessons/{lesson_id} → 200 с тем же занятием"
    )
    body = {
        "group_id": ids["G1"],
        "instructor_id": ids["I1"],
        "classroom_id": ids["C1"],
        "format": "IN_PERSON",
        "starts_at": "2026-12-02T07:00:00Z",
        "ends_at": "2026-12-02T08:30:00Z",
    }

    created = await api.post("/lessons", json=body)
    record.actual(f"POST /lessons → {created.status_code}: {created.text[:300]}")
    assert created.status_code == 201, created.text
    payload = created.json()
    contract.assert_schema(payload, "LessonWithWarnings")

    lesson = payload["lesson"]
    assert lesson["status"] == "PLANNED"
    assert lesson["rescheduled_from_id"] is None
    assert lesson["cancel_reason"] is None
    assert lesson["group_id"] == ids["G1"]
    assert lesson["instructor_id"] == ids["I1"]
    assert lesson["classroom_id"] == ids["C1"]
    assert lesson["format"] == "IN_PERSON"
    assert lesson["starts_at"] == "2026-12-02T07:00:00Z"
    assert lesson["ends_at"] == "2026-12-02T08:30:00Z"
    assert payload["warnings"] == []

    fetched = await api.get(f"/lessons/{lesson['id']}")
    record.actual(f"{record.record.actual}; GET → {fetched.status_code}")
    assert fetched.status_code == 200, fetched.text
    contract.assert_schema(fetched.json(), "Lesson")
    assert fetched.json() == lesson
