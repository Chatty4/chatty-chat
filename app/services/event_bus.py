import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.schemas.events import ChannelEvent

logger = logging.getLogger(__name__)


def channel_name(channel_id: uuid.UUID) -> str:
    """
    Per-channel pub/sub so each gateway subscribes only to the channels
    its connected users are in, not the full event stream.
    """
    return f"chat.events:{channel_id}"


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
