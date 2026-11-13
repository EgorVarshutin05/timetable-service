"""FR-01/02/04: создание, предварительная проверка и чтение занятия."""

from dataclasses import asdict

from fastapi import APIRouter

from app.api.dependencies import LessonId, Lessons, Session
from app.api.schemas import (
    LessonCreate,
    LessonResponse,
    LessonWithWarnings,
    ValidationResult,
    ViolationResponse,
    WarningResponse,
)
from app.services import catalog

router = APIRouter(prefix="/lessons", tags=["lessons"])


@router.post("", response_model=LessonWithWarnings, status_code=201, operation_id="createLesson")
async def create_lesson(body: LessonCreate, service: Lessons) -> LessonWithWarnings:
    lesson, report = await service.execute(body.to_draft(), dry_run=False)
    if lesson is None:
        raise RuntimeError("Создание не вернуло занятие")
    return LessonWithWarnings(
        lesson=LessonResponse.model_validate(lesson),
        warnings=[WarningResponse.model_validate(asdict(issue)) for issue in report.warnings],
    )


@router.post("/validate", response_model=ValidationResult, operation_id="validateLesson")
async def validate_lesson(body: LessonCreate, service: Lessons) -> ValidationResult:
    _, report = await service.execute(body.to_draft(), dry_run=True)
    return ValidationResult(
        ok=not report.violations,
        violations=[ViolationResponse.model_validate(asdict(issue)) for issue in report.violations],
        warnings=[WarningResponse.model_validate(asdict(issue)) for issue in report.warnings],
    )


@router.get("/{lesson_id}", response_model=LessonResponse, operation_id="getLesson")
async def get_lesson(lesson_id: LessonId, session: Session) -> LessonResponse:
    return LessonResponse.model_validate(await catalog.get_lesson(session, lesson_id))
