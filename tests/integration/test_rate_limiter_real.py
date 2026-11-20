import uuid
from collections.abc import AsyncGenerator

import pytest
from redis.asyncio import Redis

from app.clients.rate_limiter import RateLimiter, rate_limit_key
from app.core.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> AsyncGenerator[Redis]:
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


async def test_allows_up_to_the_limit_then_says_how_long_to_wait(redis):
    limiter, user_id = RateLimiter(redis), uuid.uuid4()

    allowed = [await limiter.hit("send", user_id, 3, 60) for _ in range(3)]
    refused = await limiter.hit("send", user_id, 3, 60)

    assert allowed == [0, 0, 0]
    assert 1 <= refused <= 60
    await redis.delete(rate_limit_key("send", user_id))


async def test_the_window_starts_with_the_first_request(redis):
    limiter, user_id = RateLimiter(redis), uuid.uuid4()

    await limiter.hit("send", user_id, 3, 60)
    await limiter.hit("send", user_id, 3, 60)

    assert 55 <= await redis.ttl(rate_limit_key("send", user_id)) <= 60  # not pushed back
    await redis.delete(rate_limit_key("send", user_id))


async def test_each_user_and_each_limit_has_its_own_count(redis):
    limiter, user_id, other_id = RateLimiter(redis), uuid.uuid4(), uuid.uuid4()
    await limiter.hit("send", user_id, 1, 60)

    assert await limiter.hit("send", other_id, 1, 60) == 0
    assert await limiter.hit("search", user_id, 1, 60) == 0
    assert await limiter.hit("send", user_id, 1, 60) > 0
    for name, user in (("send", user_id), ("send", other_id), ("search", user_id)):
        await redis.delete(rate_limit_key(name, user))


async def test_a_new_window_allows_again(redis):
    limiter, user_id = RateLimiter(redis), uuid.uuid4()
    await limiter.hit("send", user_id, 1, 1)
    assert await limiter.hit("send", user_id, 1, 1) > 0

    await redis.delete(rate_limit_key("send", user_id))  # what the window's expiry does

    assert await limiter.hit("send", user_id, 1, 1) == 0
    await redis.delete(rate_limit_key("send", user_id))
