import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ChannelKind(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    DM = "dm"


class TeamRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class ChannelRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class NotifyLevel(StrEnum):
    ALL = "all"
    MENTIONS = "mentions"
    NONE = "none"


class TeamMembership(BaseModel):
    team_id: uuid.UUID
    role: TeamRole
    public_channel_ids: list[uuid.UUID]


class ChannelMembership(BaseModel):
    channel_id: uuid.UUID
    team_id: uuid.UUID
    kind: ChannelKind
    archived: bool
    role: ChannelRole
    notify_level: NotifyLevel
    muted: bool
    hidden: bool


class Memberships(BaseModel):
    """GET /internal/users/{user_id}/memberships (api-internal.md, D-07)."""

    user_id: uuid.UUID
    is_active: bool
    teams: list[TeamMembership]
    channels: list[ChannelMembership]


class ChannelMember(BaseModel):
    user_id: uuid.UUID
    role: ChannelRole
    notify_level: NotifyLevel
    muted: bool


class CoreChannel(BaseModel):
    """GET /internal/channels/{channel_id} (api-internal.md)."""

    channel_id: uuid.UUID
    team_id: uuid.UUID
    kind: ChannelKind
    archived: bool
    team_deleted: bool
    members: list[ChannelMember]


class CoreAttachment(BaseModel):
    """A file from POST /internal/files/attach. object_key stays in chatty-chat (D-02)."""

    file_id: uuid.UUID
    object_key: str
    filename: str
    mime: str
    size: int
    attached_at: datetime


class CoreAttachments(BaseModel):
    attachments: list[CoreAttachment]
