import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.core import CoreClient
from app.clients.presence import PresenceStore
from app.clients.web_push import PushDedupe, WebPushClient
from app.repositories import PushSubscriptionRepository
from app.schemas.core import CoreChannel, NotifyLevel
from app.schemas.events import ChannelEvent, EventType
from app.schemas.push import PushMessage, PushNotification

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


class PushService:
    def __init__(
        self,
        session: AsyncSession,
        subscriptions: PushSubscriptionRepository,
        core: CoreClient,
        presence: PresenceStore,
        dedupe: PushDedupe,
        sender: WebPushClient,
    ) -> None:
        self._session = session
        self._subscriptions = subscriptions
        self._core = core
        self._presence = presence
        self._dedupe = dedupe
        self._sender = sender

    async def handle(self, event: ChannelEvent) -> None:
        """
        Pushes a new message to the members who want it. Raises ServiceUnavailable, SQLAlchemyError
        or RedisError when it should be retried; everything before the sends is safe to repeat.
        """
        if event.type != EventType.MESSAGE_CREATED:
            return
        if datetime.now(UTC) - event.created_at > MAX_AGE:
            return
        try:
            message = PushMessage.model_validate(event.payload.get("message"))
        except ValidationError:
            logger.warning("Skipping message.created without a valid message: seq %s", event.seq)
            return

        channel = await self._core.get_channel(event.channel_id)
        if channel is None:  # deleted since
            return
        mentions = set(message.mentions)
        users = [
            user_id
            for user_id in push_recipients(message.author_id, mentions, channel)
            if not await self._presence.is_looking(user_id)
        ]
        if not users:
            return

        async with self._session.begin():
            rows = await self._subscriptions.list_for_users(users)
        by_user = defaultdict(list)
        for row in rows:
            by_user[row.user_id].append(row)

        gone: list[str] = []
        for user_id, subscriptions in by_user.items():
            if not await self._dedupe.claim(message.id, user_id):
                continue
            data = PushNotification(
                channel_id=event.channel_id,
                message_id=message.id,
                seq=event.seq,
                mentioned=user_id in mentions,
                preview=(message.body or "")[:PREVIEW_CHARS],
            ).model_dump_json()
            for subscription in subscriptions:
                if not await self._sender.send(subscription, data):
                    gone.append(subscription.endpoint)

        if gone:
            async with self._session.begin():
                for endpoint in gone:
                    await self._subscriptions.delete_by_endpoint(endpoint)
