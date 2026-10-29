import uuid
from datetime import datetime
from typing import Annotated

import emoji as emoji_lib
from pydantic import AfterValidator, BaseModel, Field


def check_emoji(value: str) -> str:
    """One emoji (👍, 👍🏽, 👨‍👩‍👧), max 32 bytes (api-chat.md). Otherwise 400 {"emoji": "invalid"}."""
    if len(value.encode()) > 32 or not emoji_lib.is_emoji(value):
        raise ValueError("not one emoji")
    return value


Emoji = Annotated[str, AfterValidator(check_emoji)]


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


class ReactionsResponse(BaseModel):
    """The answer of PUT and DELETE reactions: every reaction of the message."""

    message_id: uuid.UUID
    reactions: list[ReactionResponse]


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
