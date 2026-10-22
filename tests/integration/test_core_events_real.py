import asyncio
import json
import uuid
from contextlib import suppress

import pytest
from redis.asyncio import Redis

from app.clients.core import channel_key, members_key
from app.clients.core_events import CORE_EVENTS, listen_core_events
from app.core.config import get_settings

pytestmark = pytest.mark.integration


async def wait_until_gone(redis: Redis, key: str) -> None:
    async with asyncio.timeout(5):
        while await redis.exists(key):  # noqa
            await asyncio.sleep(0.05)


async def test_subscribe_clears_cache_then_events_delete_keys():
    redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    await redis.set(members_key(user_id), "{}")
    await redis.set(channel_key(channel_id), "{}")
    task = asyncio.create_task(listen_core_events(redis))
    try:
        # Cleared on subscribe.
        await wait_until_gone(redis, members_key(user_id))
        await wait_until_gone(redis, channel_key(channel_id))

        await redis.set(members_key(user_id), "{}")
        event = {
            "type": "user.deactivated",
            "occurred_at": "2026-10-22T12:00:00Z",
            "data": {"user_id": str(user_id)},
        }
        await redis.publish(CORE_EVENTS, json.dumps(event))

        await wait_until_gone(redis, members_key(user_id))
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await redis.aclose()
