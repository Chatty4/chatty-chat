import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

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

    async def publish(self, channel_id: uuid.UUID, event: dict[str, Any]) -> None:
        """
        Push an event to everyone subscribed to this channel.
        If Redis is down, log and move on.
        """
        try:
            await self._redis.publish(channel_name(channel_id), json.dumps(event))
        except RedisError:
            logger.exception("Failed to publish event for channel %s", channel_id)
