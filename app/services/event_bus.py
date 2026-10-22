import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from app.schemas.events import ChannelEvent

logger = logging.getLogger(__name__)


def _channel_name(channel_id: uuid.UUID) -> str:
    """
    Per-channel pub/sub so each gateway subscribes only to the channels
    its connected users are in, not the full event stream.
    """
    return f"chat.events:{channel_id}"


class EventBus:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, channel_id: uuid.UUID, event: dict[str, Any]) -> None:
        """
        Push an event to everyone subscribed to this channel.
        If Redis is down, log and move on.
        """
        try:
            await self._redis.publish(_channel_name(channel_id), json.dumps(event))
        except RedisError:
            logger.exception("Failed to publish event for channel %s", channel_id)

    async def subscribe(self, channel_ids: list[uuid.UUID]) -> AsyncGenerator[ChannelEvent]:
        """
        Listen for events on the given channels and yield them one by one.
        Cancel the enclosing task when you want to stop.
        """
        pubsub: PubSub = self._redis.pubsub()
        channels = [_channel_name(cid) for cid in channel_ids]

        await pubsub.subscribe(*channels)

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    yield ChannelEvent.model_validate_json(message["data"])
                except Exception:
                    logger.exception("Malformed event on %s", message.get("channel"))
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()
