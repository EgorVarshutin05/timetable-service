"""FR-14: атомарное создание серии, HTTP-контракт из исходного OpenAPI."""

from dataclasses import asdict

from fastapi import APIRouter

from app.api.dependencies import Lessons
from app.api.schemas import (
    LessonResponse,
    LessonSeriesCreate,
    LessonSeriesResult,
    LessonWithWarnings,
    WarningResponse,
)

router = APIRouter(prefix="/lesson-series", tags=["lessons"])


@router.post(
    "", response_model=LessonSeriesResult, status_code=201, operation_id="createLessonSeries"
)
async def create_series(body: LessonSeriesCreate, service: Lessons) -> LessonSeriesResult:
    series_id, items = await service.create_series(body.to_plan())
    return LessonSeriesResult(
        series_id=series_id,
        items=[
            LessonWithWarnings(
                lesson=LessonResponse.model_validate(lesson),
                warnings=[
                    WarningResponse.model_validate(asdict(issue)) for issue in report.warnings
                ],
            )
            for lesson, report in items
        ],
    )
