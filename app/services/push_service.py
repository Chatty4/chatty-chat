import logging
import uuid
from datetime import timedelta

from app.schemas.core import CoreChannel, NotifyLevel

logger = logging.getLogger(__name__)

MAX_AGE = timedelta(minutes=10)  # a notification this late is noise
PREVIEW_CHARS = 100


def push_recipients(
    author_id: uuid.UUID, mentions: set[uuid.UUID], channel: CoreChannel
) -> list[uuid.UUID]:
    """The members who want a push for this message, by their channel settings only."""
    if channel.archived or channel.team_deleted:
        return []
    return [
        member.user_id
        for member in channel.members
        if member.user_id != author_id
        and not member.muted
        and (
            member.notify_level == NotifyLevel.ALL
            or (member.notify_level == NotifyLevel.MENTIONS and member.user_id in mentions)
        )
    ]
