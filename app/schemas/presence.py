import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class PresenceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class PresenceFrame(BaseModel):
    """The presence message on the WebSocket (events.md)."""

    type: Literal["presence"] = "presence"
    team_id: uuid.UUID
    user_id: uuid.UUID
    status: PresenceStatus


class PresenceResponse(BaseModel):
    team_id: uuid.UUID
    online: list[uuid.UUID]
