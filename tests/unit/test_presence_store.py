import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.clients.presence import PresenceStore

USER_ID, TEAM_ID = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def store() -> PresenceStore:
    """A store on a Redis that is down."""
    down = RedisConnectionError("Redis went away")
    redis = MagicMock()
    redis.pipeline.side_effect = down
    redis.sadd = AsyncMock(side_effect=down)
    redis.srem = AsyncMock(side_effect=down)
    redis.smembers = AsyncMock(side_effect=down)
    return PresenceStore(redis)


async def test_a_redis_error_never_counts_as_first_or_last(store):
    assert await store.socket_opened(USER_ID, "socket") is False
    assert await store.socket_closed(USER_ID, "socket") is False


async def test_a_redis_error_in_a_write_is_only_logged(store):
    await store.heartbeat(USER_ID, "socket")
    await store.go_online(USER_ID, [TEAM_ID])
    await store.go_offline(USER_ID, [TEAM_ID])


async def test_a_redis_error_in_the_list_gives_an_empty_list(store):
    assert await store.list_online(TEAM_ID) == []
