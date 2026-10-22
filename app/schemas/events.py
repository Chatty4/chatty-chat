import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChannelEvent(BaseModel):
    """Channel event envelope (events.md). Sent on WebSocket and written to Kafka."""

    model_config = {"extra": "ignore"}

    type: str
    channel_id: uuid.UUID
    seq: int
    created_at: datetime
    payload: dict[str, Any]
