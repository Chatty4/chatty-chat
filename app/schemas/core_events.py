import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class MembershipAction(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    SETTINGS = "settings"


class TeamMemberAction(StrEnum):
    JOINED = "joined"
    LEFT = "left"
    REMOVED = "removed"
    ROLE_CHANGED = "role_changed"


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


class MembershipChangedData(MembershipData):
    action: MembershipAction


class ChannelUpdatedData(ChannelData):
    name: str
    topic: str | None
    archived: bool


class TeamMemberChangedData(UserData):
    team_id: uuid.UUID
    action: TeamMemberAction
