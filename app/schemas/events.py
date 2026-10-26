import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class EventType(StrEnum):
    """The channel event types (events.md)."""

    MESSAGE_CREATED = "message.created"
    MESSAGE_EDITED = "message.edited"
    MESSAGE_DELETED = "message.deleted"
    REACTION_ADDED = "reaction.added"
    REACTION_REMOVED = "reaction.removed"
    PIN_ADDED = "pin.added"
    PIN_REMOVED = "pin.removed"


class ChannelEvent(BaseModel):
    """Channel event envelope (events.md). Sent on WebSocket and written to Kafka."""

    model_config = {"extra": "ignore"}

    type: str
    channel_id: uuid.UUID
    seq: int
    created_at: datetime
    payload: dict[str, Any]
