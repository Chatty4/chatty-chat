import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, ReadStateServiceDep
from app.api.rate_limits import LIMIT_READ, LIMIT_READS
from app.schemas.read_state import (
    ChannelReadsResponse,
    MarkReadRequest,
    MarkReadResponse,
    UnreadResponse,
)

router = APIRouter()


@router.post(
    "/channels/{channel_id}/read", response_model=MarkReadResponse, dependencies=[LIMIT_READ]
)
async def mark_read(
    channel_id: uuid.UUID, body: MarkReadRequest, user: CurrentUser, service: ReadStateServiceDep
) -> MarkReadResponse:
    return await service.mark_read(user, channel_id, body.seq)


@router.get("/unread", response_model=UnreadResponse)
async def list_unread(
    team_id: uuid.UUID, user: CurrentUser, service: ReadStateServiceDep
) -> UnreadResponse:
    return await service.list_unread(user, team_id)


@router.get(
    "/channels/{channel_id}/reads",
    response_model=ChannelReadsResponse,
    dependencies=[LIMIT_READS],
)
async def list_reads(
    channel_id: uuid.UUID, user: CurrentUser, service: ReadStateServiceDep
) -> ChannelReadsResponse:
    return await service.list_reads(user, channel_id)
