import uuid

from pydantic import BaseModel, Field


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


class MemberRead(BaseModel):
    user_id: uuid.UUID
    last_read_seq: int


class ChannelReadsResponse(BaseModel):
    channel_id: uuid.UUID
    last_seq: int
    reads: list[MemberRead]
