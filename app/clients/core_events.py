import asyncio
import logging

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.clients.core import CHANNEL_KEYS, MEMBERS_KEYS, channel_key, members_key
from app.schemas.core_events import ChannelData, CoreEvent, MembershipData, UserData

logger = logging.getLogger(__name__)

CORE_EVENTS = "core.events"
MAX_BACKOFF_SECONDS = 30
SCAN_BATCH = 500


async def delete_matching(redis: Redis, pattern: str) -> None:
    batch: list[str] = []
    async for key in redis.scan_iter(match=pattern, count=SCAN_BATCH):
        batch.append(key)
        if len(batch) == SCAN_BATCH:
            await redis.delete(*batch)
            batch.clear()
    if batch:
        await redis.delete(*batch)


async def clear_cache(redis: Redis) -> None:
    await delete_matching(redis, MEMBERS_KEYS)
    await delete_matching(redis, CHANNEL_KEYS)


async def handle_event(redis: Redis, raw: str) -> None:
    """Deletes the cache keys one event makes stale (api-internal.md, Caching)."""
    try:
        event = CoreEvent.model_validate_json(raw)
        match event.type:
            case "membership.changed":
                data = MembershipData.model_validate(event.data)
                await redis.delete(members_key(data.user_id), channel_key(data.channel_id))
            case "team.member.changed" | "user.deactivated":
                data = UserData.model_validate(event.data)
                await redis.delete(members_key(data.user_id))
            case "channel.updated":
                data = ChannelData.model_validate(event.data)
                await redis.delete(channel_key(data.channel_id))
            case "team.deleted":
                # Only team_id is sent. Team deletes are rare, so drop every channel key
                # instead of reading each cached body to match its team_id.
                await delete_matching(redis, CHANNEL_KEYS)
            case _:
                logger.warning("Ignoring core event of unknown type %r", event.type)
    except ValidationError:
        logger.warning("Ignoring malformed core event: %.200s", raw)


async def listen_core_events(redis: Redis) -> None:
    """Runs until cancelled and survives Redis restarts (core-events.md, Missed events)."""
    backoff = 1
    while True:
        try:
            async with redis.pubsub() as pubsub:  # type: ignore
                await pubsub.subscribe(CORE_EVENTS)
                # clear after subscribing, so no event can slip in between
                await clear_cache(redis)
                logger.info("Subscribed to %s, cache cleared", CORE_EVENTS)
                backoff = 1
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        await handle_event(redis, message["data"])
        except RedisError:
            logger.warning("Lost %s, retrying in %s s", CORE_EVENTS, backoff, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
