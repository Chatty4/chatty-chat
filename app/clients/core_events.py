import asyncio
import logging

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.clients.core import (
    ALL_VERSION,
    CHANNEL_KEYS,
    MEMBERS_KEYS,
    VERSION_TTL_SECONDS,
    channel_key,
    members_key,
    version_key,
)
from app.schemas.core_events import ChannelData, CoreEvent, MembershipData, UserData

logger = logging.getLogger(__name__)

CORE_EVENTS = "core.events"
MAX_BACKOFF_SECONDS = 30
SCAN_BATCH = 500


async def mark_stale(redis: Redis, *keys: str) -> None:
    """
    Bump the versions before the DEL, in one transaction: an old answer written before is
    deleted, one written after is refused.
    """
    async with redis.pipeline(transaction=True) as transaction:
        for key in keys:
            transaction.incr(version_key(key))
            transaction.expire(version_key(key), VERSION_TTL_SECONDS)
        transaction.delete(*keys)
        await transaction.execute()


async def mark_all_stale(redis: Redis) -> None:
    """Refuses every cache write already in progress. Call it before deleting keys."""
    await redis.incr(ALL_VERSION)


async def delete_matching(redis: Redis, pattern: str) -> None:
    # SCAN, never KEYS: KEYS blocks Redis while it walks every key.
    batch: list[str] = []
    async for key in redis.scan_iter(match=pattern, count=SCAN_BATCH):
        batch.append(key)
        if len(batch) == SCAN_BATCH:
            await redis.delete(*batch)
            batch.clear()
    if batch:
        await redis.delete(*batch)


async def clear_cache(redis: Redis) -> None:
    await mark_all_stale(redis)  # first, so a write racing the scan is refused
    await delete_matching(redis, MEMBERS_KEYS)
    await delete_matching(redis, CHANNEL_KEYS)


async def handle_event(redis: Redis, raw: str) -> None:
    """Marks the cache keys one event makes stale (api-internal.md, Caching)."""
    try:
        event = CoreEvent.model_validate_json(raw)
        match event.type:
            case "membership.changed":
                data = MembershipData.model_validate(event.data)
                await mark_stale(redis, members_key(data.user_id), channel_key(data.channel_id))
            case "team.member.changed" | "user.deactivated":
                data = UserData.model_validate(event.data)
                await mark_stale(redis, members_key(data.user_id))
            case "channel.updated":
                data = ChannelData.model_validate(event.data)
                await mark_stale(redis, channel_key(data.channel_id))
            case "team.deleted":
                # only team_id is sent, so the team's channels are unknown
                await mark_all_stale(redis)
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
