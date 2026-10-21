import logging
import uuid

import httpx
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.exceptions import ServiceUnavailable
from app.schemas.core import CoreChannel, Memberships

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300


# CHAT-187 deletes the same keys when chatty-core's events arrive.
def members_key(user_id: uuid.UUID) -> str:
    return f"members:{user_id}"


def channel_key(channel_id: uuid.UUID) -> str:
    return f"channel:{channel_id}"


class CoreClient:
    """chatty-core's internal API with Redis cache (api-internal.md, Caching)."""

    def __init__(self, http: httpx.AsyncClient, redis: Redis) -> None:
        self._http = http
        self._redis = redis

    async def get_memberships(self, user_id: uuid.UUID) -> Memberships | None:
        path = f"/internal/users/{user_id}/memberships"
        return await self._get(members_key(user_id), path, Memberships)

    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        path = f"/internal/channels/{channel_id}"
        return await self._get(channel_key(channel_id), path, CoreChannel)

    async def _get[T: BaseModel](self, key: str, path: str, model: type[T]) -> T | None:
        body = await self._read_cache(key)
        if body is None:
            body = await self._fetch(path)
            if body is None:
                return None
            await self._write_cache(key, body)
        return model.model_validate_json(body)

    async def _fetch(self, path: str) -> str | None:
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:  # connection refused, timeout
            logger.warning("chatty-core unreachable: GET %s", path, exc_info=True)
            raise ServiceUnavailable() from exc
        if response.status_code == 404:
            return None
        if response.is_error:
            # 401 here means CORE_SERVICE_TOKEN differs between the two services
            logger.error("chatty-core answered %s: GET %s", response.status_code, path)
            raise ServiceUnavailable()
        return response.text

    async def _read_cache(self, key: str) -> bytes | str | None:
        try:
            return await self._redis.get(key)
        except RedisError:
            logger.warning("Cache read failed, asking chatty-core: %s", key, exc_info=True)
            return None

    async def _write_cache(self, key: str, body: str) -> None:
        try:
            await self._redis.set(key, body, ex=CACHE_TTL_SECONDS)
        except RedisError:
            logger.warning("Cache write failed: %s", key, exc_info=True)
