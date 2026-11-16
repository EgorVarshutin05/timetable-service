"""FR-11/16: получение группы и страница, отсортированная по name/id."""

from fastapi import APIRouter

from app.api.dependencies import GroupId, GroupPagination, Session
from app.api.schemas import GroupPage, GroupResponse, group_query_parameters
from app.services import catalog

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get(
    "",
    response_model=GroupPage,
    operation_id="listGroups",
    openapi_extra={"parameters": group_query_parameters()},
)
async def list_groups(pagination: GroupPagination, session: Session) -> GroupPage:
    items, total = await catalog.list_groups(session, pagination.limit, pagination.offset)
    return GroupPage(items=[GroupResponse.model_validate(item) for item in items], total=total)


@router.get("/{group_id}", response_model=GroupResponse, operation_id="getGroup")
async def get_group(group_id: GroupId, session: Session) -> GroupResponse:
    return GroupResponse.model_validate(await catalog.get_group(session, group_id))
