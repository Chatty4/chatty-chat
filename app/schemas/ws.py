import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StrictBool, TypeAdapter


class Heartbeat(BaseModel):
    """Client -> server heartbeat (api-chat.md, GET /ws)."""

    type: Literal["heartbeat"]
    visible: StrictBool


class Watch(BaseModel):
    type: Literal["watch"]
    channel_id: uuid.UUID


class Unwatch(BaseModel):
    type: Literal["unwatch"]
    channel_id: uuid.UUID


class Typing(BaseModel):
    type: Literal["typing"]
    channel_id: uuid.UUID


class TypingFrame(BaseModel):
    """Server -> client: someone is typing (events.md, typing)."""

    type: Literal["typing"] = "typing"
    channel_id: uuid.UUID
    user_id: uuid.UUID


class UnreadMessage(BaseModel):
    """Server -> client: new counts of one channel (events.md, unread)"""

    type: Literal["unread"] = "unread"
    channel_id: uuid.UUID
    unread: int
    mentions: int


ClientMessage = TypeAdapter(
    Annotated[Heartbeat | Typing | Watch | Unwatch, Field(discriminator="type")]
)
