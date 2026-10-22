import logging

from pydantic import ValidationError
from redis.asyncio import Redis

from app.clients.core import CHANNEL_KEYS, MEMBERS_KEYS, channel_key, members_key
from app.schemas.core_events import ChannelData, CoreEvent, MembershipData, UserData

logger = logging.getLogger(__name__)


async def delete_matching(redis: Redis, pattern: str) -> None:
    # SCAN, never KEYS: KEYS blocks Redis while it walks every key.
    keys = [key async for key in redis.scan_iter(match=pattern, count=500)]
    if keys:
        await redis.delete(*keys)


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
