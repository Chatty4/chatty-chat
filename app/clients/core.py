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
VERSION_TTL_SECONDS = 86_400  # far longer than any request, so no counter expires mid-request
ALL_VERSION = "version:all"  # bumped when every key may be stale
NO_VERSION = "0"  # same as the '0' in the Lua script

# SET only if no event bumped a version meanwhile. A script runs as one step in Redis.
_SET_IF_UNCHANGED = """
if (redis.call('GET', KEYS[2]) or '0') == ARGV[3]
   and (redis.call('GET', KEYS[3]) or '0') == ARGV[4] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""


def members_key(user_id: uuid.UUID) -> str:
    return f"members:{user_id}"


def channel_key(channel_id: uuid.UUID) -> str:
    return f"channel:{channel_id}"


def version_key(cache_key: str) -> str:
    """Outside members:* and channel:*, so clearing the cache keeps the counters."""
    return f"version:{cache_key}"


MEMBERS_KEYS = members_key("*")  # type: ignore[arg-type]
CHANNEL_KEYS = channel_key("*")  # type: ignore[arg-type]


class CoreClient:
    def __init__(self, http: httpx.AsyncClient, redis: Redis) -> None:
        self._http = http
        self._redis = redis
        self._set_if_unchanged = redis.register_script(_SET_IF_UNCHANGED)

    async def get_memberships(self, user_id: uuid.UUID) -> Memberships | None:
        path = f"/internal/users/{user_id}/memberships"
        return await self._get(members_key(user_id), path, Memberships)

    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        path = f"/internal/channels/{channel_id}"
        return await self._get(channel_key(channel_id), path, CoreChannel)

    async def _get[T: BaseModel](self, key: str, path: str, model: type[T]) -> T | None:
        body, version, all_version = await self._read_cache(key)
        if body is None:
            body = await self._fetch(path)
            if body is None:
                return None
            if version is not None and all_version is not None:
                await self._write_cache(key, body, version, all_version)
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

    async def _read_cache(
        self, key: str
    ) -> tuple[bytes | str | None, bytes | str | None, bytes | str | None]:
        """
        Reads the versions before chatty-core is asked, so _write_cache can spot an event in
        between. All None if Redis fails: then nothing is cached.
        """
        try:
            body, version, all_version = await self._redis.mget(key, version_key(key), ALL_VERSION)
        except RedisError:
            logger.warning("Cache read failed, asking chatty-core: %s", key, exc_info=True)
            return None, None, None
        return body, version or NO_VERSION, all_version or NO_VERSION

    async def _write_cache(
        self, key: str, body: bytes | str, version: bytes | str, all_version: bytes | str
    ) -> None:
        try:
            await self._set_if_unchanged(
                keys=[key, version_key(key), ALL_VERSION],
                args=[body, CACHE_TTL_SECONDS, version, all_version],
            )
        except RedisError:
            logger.warning("Cache write failed: %s", key, exc_info=True)
