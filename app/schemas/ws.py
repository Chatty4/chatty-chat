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


ClientMessage = TypeAdapter(Annotated[Heartbeat | Watch | Unwatch, Field(discriminator="type")])
