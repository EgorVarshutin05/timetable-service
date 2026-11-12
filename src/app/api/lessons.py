"""FR-04: получение занятия."""

from fastapi import APIRouter

from app.api.dependencies import LessonId, Session
from app.api.schemas import LessonResponse
from app.services import catalog

router = APIRouter(prefix="/lessons", tags=["lessons"])


@router.get("/{lesson_id}", response_model=LessonResponse, operation_id="getLesson")
async def get_lesson(lesson_id: LessonId, session: Session) -> LessonResponse:
    return LessonResponse.model_validate(await catalog.get_lesson(session, lesson_id))
