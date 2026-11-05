import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, ReadStateServiceDep
from app.schemas.read_state import MarkReadRequest, MarkReadResponse

router = APIRouter()


@router.post("/channels/{channel_id}/read", response_model=MarkReadResponse)
async def mark_read(
    channel_id: uuid.UUID, body: MarkReadRequest, user: CurrentUser, service: ReadStateServiceDep
) -> MarkReadResponse:
    return await service.mark_read(user, channel_id, body.seq)
