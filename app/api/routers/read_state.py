import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, ReadStateServiceDep
from app.schemas.read_state import MarkReadRequest, MarkReadResponse, UnreadResponse

router = APIRouter()


@router.post("/channels/{channel_id}/read", response_model=MarkReadResponse)
async def mark_read(
    channel_id: uuid.UUID, body: MarkReadRequest, user: CurrentUser, service: ReadStateServiceDep
) -> MarkReadResponse:
    return await service.mark_read(user, channel_id, body.seq)


@router.get("/unread", response_model=UnreadResponse)
async def list_unread(
    team_id: uuid.UUID, user: CurrentUser, service: ReadStateServiceDep
) -> UnreadResponse:
    return await service.list_unread(user, team_id)
