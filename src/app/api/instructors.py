"""FR-15: недельный отчёт нагрузки по локальным дням преподавателя."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import InstructorId, Workloads
from app.api.schemas import WorkloadDayResponse, WorkloadResponse

router = APIRouter(prefix="/instructors", tags=["instructors"])


@router.get(
    "/{instructor_id}/workload",
    response_model=WorkloadResponse,
    operation_id="getInstructorWorkload",
)
async def get_workload(
    instructor_id: InstructorId,
    service: Workloads,
    week: Annotated[str, Query(pattern=r"^\d{4}-W\d{2}$", examples=["2026-W49"])],
) -> WorkloadResponse:
    result = await service.get(instructor_id, week)
    return WorkloadResponse(
        instructor_id=result.instructor_id,
        week=result.week,
        max_hours_per_day=float(result.max_hours_per_day),
        days=[
            WorkloadDayResponse(date=day.date, hours=float(day.hours), exceeded=day.exceeded)
            for day in result.days
        ],
    )
