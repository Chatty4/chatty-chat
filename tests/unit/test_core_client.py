import json
import uuid

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.clients.core import CACHE_TTL_SECONDS, CoreClient, members_key
from app.core.exceptions import ServiceUnavailable

USER_ID = uuid.uuid4()
MEMBERSHIPS = {"user_id": str(USER_ID), "is_active": True, "teams": [], "channels": []}


class FakeRedis:
    def __init__(self, data: dict[str, str] | None = None, error: Exception | None = None):
        self.data = dict(data or {})
        self.ttl: dict[str, int | None] = {}
        self._error = error

    async def get(self, key: str) -> str | None:
        if self._error:
            raise self._error
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self._error:
            raise self._error
        self.data[key] = value
        self.ttl[key] = ex


class FakeCoreServer:
    """An httpx transport that answers like chatty-core and counts the calls."""

    def __init__(self, status: int = 200, body: dict | None = None, error: Exception | None = None):
        self.calls = 0
        self._status = status
        self._body = body
        self._error = error

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url="http://core"
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self._error:
            raise self._error
        return httpx.Response(self._status, json=self._body)


async def test_cache_hit_does_not_call_core():
    core = FakeCoreServer(body=MEMBERSHIPS)
    redis = FakeRedis({members_key(USER_ID): json.dumps(MEMBERSHIPS)})

    result = await CoreClient(core.client(), redis).get_memberships(USER_ID)

    assert result is not None and result.user_id == USER_ID
    assert core.calls == 0


async def test_cache_miss_calls_core_once_and_stores_with_ttl():
    core = FakeCoreServer(body=MEMBERSHIPS)
    redis = FakeRedis()

    result = await CoreClient(core.client(), redis).get_memberships(USER_ID)

    assert result is not None and result.is_active
    assert core.calls == 1
    assert members_key(USER_ID) in redis.data
    assert redis.ttl[members_key(USER_ID)] == CACHE_TTL_SECONDS == 300


async def test_core_timeout_with_empty_cache_is_503():
    core = FakeCoreServer(error=httpx.ConnectTimeout("timed out"))

    with pytest.raises(ServiceUnavailable):
        await CoreClient(core.client(), FakeRedis()).get_memberships(USER_ID)


@pytest.mark.parametrize("status", [401, 500])
async def test_core_error_is_503(status):
    core = FakeCoreServer(status=status, body={"error": {"code": "x", "message": "x"}})

    with pytest.raises(ServiceUnavailable):
        await CoreClient(core.client(), FakeRedis()).get_memberships(USER_ID)


async def test_not_found_returns_none_and_is_not_cached():
    core = FakeCoreServer(status=404, body={"error": {"code": "user_not_found", "message": "x"}})
    redis = FakeRedis()

    assert await CoreClient(core.client(), redis).get_memberships(USER_ID) is None
    assert redis.data == {}


async def test_redis_down_falls_through_to_core():
    core = FakeCoreServer(body=MEMBERSHIPS)
    redis = FakeRedis(error=RedisConnectionError())

    result = await CoreClient(core.client(), redis).get_memberships(USER_ID)

    assert result is not None
    assert core.calls == 1
