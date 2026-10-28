import json
import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest
from redis.asyncio import Redis

from app.clients.core import CoreClient, members_key, version_key
from app.clients.core_events import mark_all_stale, mark_stale
from app.core.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def user_id(redis):
    """A fresh user, and its cache key and counter deleted afterwards."""
    user_id = uuid.uuid4()
    yield user_id
    key = members_key(user_id)
    await redis.delete(key, version_key(key))


def core_answering(
    user_id: uuid.UUID, during: Callable[[], Awaitable[None]] | None = None
) -> httpx.AsyncClient:
    """A fake chatty-core; `during` runs while it answers, like an event arriving meanwhile."""
    body = {"user_id": str(user_id), "is_active": True, "teams": [], "channels": []}

    async def handle(request: httpx.Request) -> httpx.Response:
        if during is not None:
            await during()
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://core")


async def test_answer_is_cached_when_nothing_changed(redis, user_id):
    await CoreClient(core_answering(user_id), redis).get_memberships(user_id)

    assert json.loads(await redis.get(members_key(user_id)))["user_id"] == str(user_id)


async def test_event_during_the_request_keeps_the_old_answer_out(redis, user_id):
    key = members_key(user_id)
    http = core_answering(user_id, during=lambda: mark_stale(redis, key))

    result = await CoreClient(http, redis).get_memberships(user_id)

    assert result is not None
    assert await redis.get(key) is None


async def test_global_bump_during_the_request_keeps_the_old_answer_out(redis, user_id):
    http = core_answering(user_id, during=lambda: mark_all_stale(redis))

    await CoreClient(http, redis).get_memberships(user_id)

    assert await redis.get(members_key(user_id)) is None
