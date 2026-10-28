import uuid
from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.events import ChannelEvent


class SyncRequest(BaseModel):
    cursors: dict[uuid.UUID, Annotated[int, Field(ge=0)]] = Field(max_length=500)


class ChannelSyncResult(BaseModel):
    events: list[ChannelEvent]
    last_seq: int
    gap_too_large: bool


class ChannelSyncError(BaseModel):
    error: str


class SyncResponse(BaseModel):
    channels: dict[uuid.UUID, ChannelSyncResult | ChannelSyncError]


class ChannelEventsResponse(BaseModel):
    events: list[ChannelEvent]
    last_seq: int
    has_more: bool
