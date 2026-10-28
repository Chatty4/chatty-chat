import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SendMessageRequest(BaseModel):
    client_msg_id: uuid.UUID
    body: str = Field("", max_length=4000)
    file_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class AttachmentResponse(BaseModel):
    file_id: uuid.UUID
    filename: str
    mime: str
    size: int


class ReactionResponse(BaseModel):
    emoji: str
    count: int
    me: bool


class MessageResponse(BaseModel):
    """The message object. Also the payload of message.created."""

    id: uuid.UUID
    channel_id: uuid.UUID
    seq: int
    author_id: uuid.UUID
    body: str
    file_ids: list[uuid.UUID]
    attachments: list[AttachmentResponse]
    mentions: list[uuid.UUID]
    reactions: list[ReactionResponse]
    pinned: bool
    edited_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime


class MessageHistoryResponse(BaseModel):
    messages: list[MessageResponse]
    has_more: bool
    last_seq: int


class EditMessageRequest(BaseModel):
    body: str = Field(max_length=4000)
