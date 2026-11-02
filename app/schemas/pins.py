import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.messages import MessageResponse


class PinResponse(BaseModel):
    """The answer of POST pins. Without channel_id it is also the pin.added payload."""

    channel_id: uuid.UUID
    message_id: uuid.UUID
    pinned_by: uuid.UUID
    pinned_at: datetime


class PinnedMessage(BaseModel):
    pinned_by: uuid.UUID
    pinned_at: datetime
    message: MessageResponse


class PinsResponse(BaseModel):
    pins: list[PinnedMessage]
