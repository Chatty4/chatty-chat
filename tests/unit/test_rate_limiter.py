import logging
import uuid

from redis.asyncio import Redis

from app.clients.rate_limiter import RateLimiter


async def test_redis_down_allows_the_request_and_logs_an_error(caplog):
    # nothing listens on port 1, so every command fails at once
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    try:
        with caplog.at_level(logging.ERROR, logger="app.clients.rate_limiter"):
            assert await RateLimiter(redis).hit("send", uuid.uuid4(), 1, 60) == 0
    finally:
        await redis.aclose()

    assert "allowing the request" in caplog.text
