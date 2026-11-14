"""HTTP-контракт занятий: расписание, валидация и команды жизненного цикла."""

from dataclasses import asdict

from fastapi import APIRouter

from app.api.dependencies import Filters, LessonId, Lessons, Session
from app.api.schemas import (
    CancelRequest,
    LessonCreate,
    LessonPage,
    LessonPatch,
    LessonResponse,
    LessonWithWarnings,
    RescheduleRequest,
    ValidationResult,
    ViolationResponse,
    WarningResponse,
    list_query_parameters,
)
from app.services import catalog

router = APIRouter(prefix="/lessons", tags=["lessons"])


@router.get(
    "",
    response_model=LessonPage,
    operation_id="listLessons",
    openapi_extra={"parameters": list_query_parameters()},
)
async def list_lessons(filters: Filters, service: Lessons) -> LessonPage:
    items, total = await service.list_lessons(filters)
    return LessonPage(items=[LessonResponse.model_validate(item) for item in items], total=total)


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


@router.patch("/{lesson_id}", response_model=LessonWithWarnings, operation_id="updateLesson")
async def update_lesson(
    lesson_id: LessonId, body: LessonPatch, service: Lessons
) -> LessonWithWarnings:
    lesson, report = await service.patch(lesson_id, body.apply)
    return LessonWithWarnings(
        lesson=LessonResponse.model_validate(lesson),
        warnings=[WarningResponse.model_validate(asdict(issue)) for issue in report.warnings],
    )


@router.post("/{lesson_id}/confirm", response_model=LessonResponse, operation_id="confirmLesson")
async def confirm_lesson(lesson_id: LessonId, service: Lessons) -> LessonResponse:
    return LessonResponse.model_validate(await service.confirm(lesson_id))


@router.post("/{lesson_id}/cancel", response_model=LessonResponse, operation_id="cancelLesson")
async def cancel_lesson(
    lesson_id: LessonId, service: Lessons, body: CancelRequest | None = None
) -> LessonResponse:
    return LessonResponse.model_validate(
        await service.cancel(lesson_id, body.reason if body is not None else None)
    )


@router.post("/{lesson_id}/complete", response_model=LessonResponse, operation_id="completeLesson")
async def complete_lesson(lesson_id: LessonId, service: Lessons) -> LessonResponse:
    return LessonResponse.model_validate(await service.complete(lesson_id))


@router.post(
    "/{lesson_id}/reschedule",
    response_model=LessonWithWarnings,
    status_code=201,
    operation_id="rescheduleLesson",
)
async def reschedule_lesson(
    lesson_id: LessonId, body: RescheduleRequest, service: Lessons
) -> LessonWithWarnings:
    lesson, report = await service.reschedule(lesson_id, body.apply)
    return LessonWithWarnings(
        lesson=LessonResponse.model_validate(lesson),
        warnings=[WarningResponse.model_validate(asdict(issue)) for issue in report.warnings],
    )
