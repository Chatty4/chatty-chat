import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def rate_limit_key(name: str, user_id: uuid.UUID) -> str:
    return f"rate:{name}:{user_id}"


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, name: str, user_id: uuid.UUID, limit: int, window: int) -> int:
        """Counts one request. 0 if it is allowed, else the seconds until the window ends."""
        key = rate_limit_key(name, user_id)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.incr(key)
                pipe.expire(key, window, nx=True)
                count, _ = await pipe.execute()
            if count > limit:
                return max(await self._redis.ttl(key), 1)
        except RedisError:
            # error, not warning: requests now go through without a limit
            logger.error("Rate limit check failed for %s, allowing the request", key)
        return 0
