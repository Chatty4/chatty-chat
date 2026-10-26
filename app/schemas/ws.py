from typing import Literal

from pydantic import BaseModel, StrictBool


class Heartbeat(BaseModel):
    """Client -> server heartbeat (api-chat.md, GET /ws). CHAT-185 adds the other messages."""

    type: Literal["heartbeat"]
    visible: StrictBool
