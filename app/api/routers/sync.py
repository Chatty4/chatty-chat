import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SyncServiceDep
from app.api.rate_limits import LIMIT_EVENTS, LIMIT_SYNC
from app.schemas.sync import ChannelEventsResponse, SyncRequest, SyncResponse

router = APIRouter()


@router.post("/sync", response_model=SyncResponse, dependencies=[LIMIT_SYNC])
async def sync(data: SyncRequest, user: CurrentUser, service: SyncServiceDep) -> SyncResponse:
    return await service.sync(user, data.cursors)


@router.get(
    "/channels/{channel_id}/events",
    response_model=ChannelEventsResponse,
    dependencies=[LIMIT_EVENTS],
)
async def list_channel_events(
    channel_id: uuid.UUID,
    user: CurrentUser,
    service: SyncServiceDep,
    after_seq: Annotated[int, Query(ge=0)],
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> ChannelEventsResponse:
    return await service.list_channel_events(user, channel_id, after_seq, limit)
