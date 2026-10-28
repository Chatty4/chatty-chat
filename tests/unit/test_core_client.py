import json
import uuid
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.clients.core import ALL_VERSION, CACHE_TTL_SECONDS, CoreClient, members_key, version_key
from app.core.exceptions import ServiceUnavailable

USER_ID = uuid.uuid4()
KEY = members_key(USER_ID)
MEMBERSHIPS = {"user_id": str(USER_ID), "is_active": True, "teams": [], "channels": []}


class FakeRedis:
    def __init__(self, data: dict[str, str] | None = None, error: Exception | None = None):
        self.data = dict(data or {})
        self.ttl: dict[str, int] = {}
        self._error = error

    async def mget(self, *keys: str) -> list[str | None]:
        if self._error:
            raise self._error
        return [self.data.get(key) for key in keys]

    def register_script(self, script: str) -> Callable:
        """Does what the Lua script does; the script itself is tested against the real Redis."""

        async def set_if_unchanged(keys: list[str], args: list) -> int:
            if self._error:
                raise self._error
            key, key_version, all_version = keys
            body, ttl, version_read, all_version_read = args
            if self.data.get(key_version, "0") != version_read:
                return 0
            if self.data.get(all_version, "0") != all_version_read:
                return 0
            self.data[key] = body
            self.ttl[key] = ttl
            return 1

        return set_if_unchanged

    @property
    def as_redis(self) -> Redis:
        """The fake only duck-types Redis, so cast it for the type checker."""
        return cast(Redis, self)


class FakeCoreServer:
    """An httpx transport that answers like chatty-core and counts the calls."""

    def __init__(
        self,
        status: int = 200,
        body: dict | None = None,
        error: Exception | None = None,
        on_request: Callable[[], None] | None = None,
    ):
        self.calls = 0
        self._status = status
        self._body = body
        self._error = error
        self._on_request = on_request  # runs while chatty-core is answering

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url="http://core"
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self._on_request:
            self._on_request()
        if self._error:
            raise self._error
        return httpx.Response(self._status, json=self._body)


async def test_cache_hit_does_not_call_core():
    core = FakeCoreServer(body=MEMBERSHIPS)
    redis = FakeRedis({KEY: json.dumps(MEMBERSHIPS)})

    result = await CoreClient(core.client(), redis.as_redis).get_memberships(USER_ID)

    assert result is not None and result.user_id == USER_ID
    assert core.calls == 0


async def test_cache_miss_calls_core_once_and_stores_with_ttl():
    core = FakeCoreServer(body=MEMBERSHIPS)
    redis = FakeRedis()

    result = await CoreClient(core.client(), redis.as_redis).get_memberships(USER_ID)

    assert result is not None and result.is_active
    assert core.calls == 1
    assert KEY in redis.data
    assert redis.ttl[KEY] == CACHE_TTL_SECONDS == 300


async def test_answer_after_earlier_events_is_cached():
    redis = FakeRedis({version_key(KEY): "3", ALL_VERSION: "2"})  # events happened before

    await CoreClient(FakeCoreServer(body=MEMBERSHIPS).client(), redis.as_redis).get_memberships(
        USER_ID
    )

    assert KEY in redis.data


@pytest.mark.parametrize("bumped", [version_key(KEY), ALL_VERSION])
async def test_answer_read_before_an_event_is_not_cached(bumped):
    redis = FakeRedis()
    # the event arrives while chatty-core is answering
    core = FakeCoreServer(body=MEMBERSHIPS, on_request=lambda: redis.data.update({bumped: "1"}))

    result = await CoreClient(core.client(), redis.as_redis).get_memberships(USER_ID)

    assert result is not None  # this request still gets its answer
    assert KEY not in redis.data  # but it isn't cached, so the next one asks again


async def test_core_timeout_with_empty_cache_is_503():
    core = FakeCoreServer(error=httpx.ConnectTimeout("timed out"))

    with pytest.raises(ServiceUnavailable):
        await CoreClient(core.client(), FakeRedis().as_redis).get_memberships(USER_ID)


@pytest.mark.parametrize("status", [401, 500])
async def test_core_error_is_503(status):
    core = FakeCoreServer(status=status, body={"error": {"code": "x", "message": "x"}})

    with pytest.raises(ServiceUnavailable):
        await CoreClient(core.client(), FakeRedis().as_redis).get_memberships(USER_ID)


async def test_not_found_returns_none_and_is_not_cached():
    core = FakeCoreServer(status=404, body={"error": {"code": "user_not_found", "message": "x"}})
    redis = FakeRedis()

    assert await CoreClient(core.client(), redis.as_redis).get_memberships(USER_ID) is None
    assert redis.data == {}


async def test_redis_down_falls_through_to_core_without_caching():
    core = FakeCoreServer(body=MEMBERSHIPS)
    redis = FakeRedis(error=RedisConnectionError())

    result = await CoreClient(core.client(), redis.as_redis).get_memberships(USER_ID)

    assert result is not None
    assert core.calls == 1
    assert redis.data == {}
