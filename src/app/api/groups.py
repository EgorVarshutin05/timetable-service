"""FR-11: получение группы."""

from fastapi import APIRouter

from app.api.dependencies import GroupId, Session
from app.api.schemas import GroupResponse
from app.services import catalog

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("/{group_id}", response_model=GroupResponse, operation_id="getGroup")
async def get_group(group_id: GroupId, session: Session) -> GroupResponse:
    return GroupResponse.model_validate(await catalog.get_group(session, group_id))
