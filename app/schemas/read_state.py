
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.messages import MessageResponse

class MarkReadRequest(BaseModel):
    seq: int = Field(ge=0)

class ChannelUnread(BaseModel):
    channel_id: uuid.UUID
    unread: int
    mentions: int

class MarkReadResponse(ChannelUnread):
    last_read_seq: int

class UnreadResponse(BaseModel):
    channels: list[ChannelUnread]
    