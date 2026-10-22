import uuid
from typing import Any

from pydantic import BaseModel


class CoreEvent(BaseModel):
    """The envelope on core.events (core-events.md). Unknown fields are ignored"""

    type: str
    data: dict[str, Any]


class UserData(BaseModel):
    user_id: uuid.UUID


class ChannelData(BaseModel):
    channel_id: uuid.UUID


class MembershipData(UserData, ChannelData):
    pass
