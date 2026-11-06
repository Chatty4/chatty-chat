import logging
import uuid

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.schemas.events import ChannelEvent

logger = logging.getLogger(__name__)

USER_CHANNEL_PREFIX = "chat.users:"
TYPING_CHANNEL_PREFIX = "chat.typing:"


def channel_name(channel_id: uuid.UUID) -> str:
    """
    Per-channel pub/sub so each gateway subscribes only to the channels
    its connected users are in, not the full event stream.
    """
    return f"chat.events:{channel_id}"


def typing_channel(channel_id: uuid.UUID) -> str:
    """Typing frames of one channel. Gateways listen to it together with channel_name."""
    return f"{TYPING_CHANNEL_PREFIX}{channel_id}"


def user_channel_name(user_id: uuid.UUID) -> str:
    """
    Per-user pub/sub for live messages to every socket of one user, on any gateway (D-12).
    Gateways subscribe to it exactly, never with a pattern.
    """
    return f"{USER_CHANNEL_PREFIX}{user_id}"


class EventBus:
    """Sends channel events. The Gateway (services/gateway.py) receives them."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, event: ChannelEvent) -> None:
        """
        Push an event to everyone subscribed to its channel. Call it only after the commit,
        so nobody gets an event for a change that isn't saved. If Redis is down, log and move on.
        """
        try:
            await self._redis.publish(channel_name(event.channel_id), event.model_dump_json())
        except RedisError:
            logger.exception("Failed to publish event for channel %s", event.channel_id)

    async def send_to_user(self, user_id: uuid.UUID, message: BaseModel) -> None:
        """
        Push a live message to every socket of one user. Call it only after the commit.
        If redis is down, log and move on: the web app calls GET /unread after it reconnects.
        """
        try:
            await self._redis.publish(user_channel_name(user_id), message.model_dump_json())
        except RedisError:
            logger.exception("Failed to publish a live message to user %s", user_id)
