import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress

import pytest
from redis.asyncio import Redis

from app.clients.presence import (
    PRESENCE_TTL_SECONDS,
    PresenceStore,
    online_key,
    presence_channel,
    presence_key,
    visible_key,
)
from app.core.config import get_settings
from app.services.connection_registry import ConnectionRegistry
from app.services.gateway import Gateway
from tests.fakes import FakeSocket

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> AsyncGenerator[Redis]:
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def team_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
async def clean_keys(redis, team_id, user_id) -> AsyncGenerator[None]:
    yield
    await redis.delete(online_key(team_id), presence_key(user_id), visible_key(user_id))


@pytest.fixture
async def start_gateway(redis, clean_keys) -> AsyncGenerator[Callable[[], Gateway]]:
    """Each call is its own gateway, like two chatty-chat processes."""
    tasks: list[asyncio.Task] = []

    def start() -> Gateway:
        gateway = Gateway(redis, ConnectionRegistry(), PresenceStore(redis))
        tasks.append(asyncio.create_task(gateway.run()))
        return gateway

    yield start
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def wait_for_subscribers(redis: Redis, team_id: uuid.UUID, count: int) -> None:
    async with asyncio.timeout(5):
        while (await redis.pubsub_numsub(presence_channel(team_id)))[0][1] != count:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def statuses(socket: FakeSocket, user_id: uuid.UUID) -> list[str]:
    frames = [frame for frame in socket.frames if frame["user_id"] == str(user_id)]
    return [frame["status"] for frame in frames]


async def wait_for_statuses(socket: FakeSocket, user_id: uuid.UUID, expected: list[str]) -> None:
    async with asyncio.timeout(5):
        while statuses(socket, user_id) != expected:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_a_user_stays_online_until_the_last_socket_on_any_gateway_closes(
    start_gateway, redis, team_id, user_id
):
    gateway_a, gateway_b = start_gateway(), start_gateway()
    watcher_id, watcher = uuid.uuid4(), FakeSocket()
    await gateway_b.connect(watcher_id, watcher, [], [team_id])
    await wait_for_subscribers(redis, team_id, 1)
    tab_a, tab_b = FakeSocket(), FakeSocket()

    await gateway_a.connect(user_id, tab_a, [], [team_id])
    await gateway_b.connect(user_id, tab_b, [], [team_id])
    await wait_for_statuses(watcher, user_id, ["online"])

    await gateway_a.disconnect(user_id, tab_a)  # gateway A has no socket of the user left
    assert user_id in await PresenceStore(redis).list_online(team_id)

    await gateway_b.disconnect(user_id, tab_b)
    await wait_for_statuses(watcher, user_id, ["online", "offline"])
    assert user_id not in await PresenceStore(redis).list_online(team_id)
    await gateway_b.disconnect(watcher_id, watcher)


async def test_the_last_two_sockets_closing_at_once_send_one_offline(
    start_gateway, redis, team_id, user_id
):
    gateway_a, gateway_b = start_gateway(), start_gateway()
    watcher_id, watcher = uuid.uuid4(), FakeSocket()
    await gateway_b.connect(watcher_id, watcher, [], [team_id])
    await wait_for_subscribers(redis, team_id, 1)
    tab_a, tab_b = FakeSocket(), FakeSocket()
    await gateway_a.connect(user_id, tab_a, [], [team_id])
    await gateway_b.connect(user_id, tab_b, [], [team_id])

    await asyncio.gather(gateway_a.disconnect(user_id, tab_a), gateway_b.disconnect(user_id, tab_b))

    await wait_for_statuses(watcher, user_id, ["online", "offline"])
    await asyncio.sleep(0.1)  # a second offline would arrive now
    assert statuses(watcher, user_id) == ["online", "offline"]
    await gateway_b.disconnect(watcher_id, watcher)


async def test_the_gateway_unsubscribes_with_the_last_socket_of_the_team(
    start_gateway, redis, team_id, user_id
):
    gateway = start_gateway()
    first, second = FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first, [], [team_id])
    await gateway.connect(user_id, second, [], [team_id])
    await wait_for_subscribers(redis, team_id, 1)

    await gateway.disconnect(user_id, first)
    await wait_for_subscribers(redis, team_id, 1)

    await gateway.disconnect(user_id, second)
    await wait_for_subscribers(redis, team_id, 0)


async def test_a_stale_line_blocks_neither_online_nor_offline(redis, clean_keys, user_id):
    store = PresenceStore(redis)
    old = time.time() - PRESENCE_TTL_SECONDS - 1
    await redis.zadd(presence_key(user_id), {"crashed": old})

    assert await store.socket_opened(user_id, "new") is True

    await redis.zadd(presence_key(user_id), {"crashed": old})
    assert await store.socket_closed(user_id, "new") is True


async def test_a_heartbeat_updates_the_line_and_resets_the_ttl(redis, clean_keys, user_id):
    store = PresenceStore(redis)
    await store.socket_opened(user_id, "socket")
    await redis.expire(presence_key(user_id), 5)
    await redis.zadd(presence_key(user_id), {"socket": 0})

    await store.heartbeat(user_id, "socket", visible=False)

    assert await redis.ttl(presence_key(user_id)) > 5
    assert await redis.zscore(presence_key(user_id), "socket") > 0


async def test_a_visible_heartbeat_adds_the_socket_and_a_hidden_one_removes_it(
    redis, clean_keys, user_id
):
    store = PresenceStore(redis)
    await store.socket_opened(user_id, "socket")

    await store.heartbeat(user_id, "socket", visible=True)
    assert await redis.zscore(visible_key(user_id), "socket") > 0
    assert 0 < await redis.ttl(visible_key(user_id)) <= PRESENCE_TTL_SECONDS

    await store.heartbeat(user_id, "socket", visible=False)
    assert await redis.zscore(visible_key(user_id), "socket") is None
    assert await redis.zscore(presence_key(user_id), "socket") is not None  # still online


async def test_a_hidden_tab_leaves_another_visible_one(redis, clean_keys, user_id):
    store = PresenceStore(redis)

    await store.heartbeat(user_id, "laptop", visible=True)
    await store.heartbeat(user_id, "phone", visible=False)

    assert await redis.zrange(visible_key(user_id), 0, -1) == ["laptop"]


async def test_a_visible_heartbeat_drops_lines_of_crashed_gateways(redis, clean_keys, user_id):
    store = PresenceStore(redis)
    await redis.zadd(visible_key(user_id), {"crashed": time.time() - PRESENCE_TTL_SECONDS - 1})

    await store.heartbeat(user_id, "socket", visible=True)

    assert await redis.zrange(visible_key(user_id), 0, -1) == ["socket"]


async def test_a_closed_socket_leaves_the_visible_tabs(redis, clean_keys, user_id):
    store = PresenceStore(redis)
    await store.socket_opened(user_id, "socket")
    await store.heartbeat(user_id, "socket", visible=True)

    assert await store.socket_closed(user_id, "socket") is True

    assert await redis.exists(visible_key(user_id)) == 0


async def test_a_closed_socket_leaves_the_other_visible_tabs(redis, clean_keys, user_id):
    store = PresenceStore(redis)
    for socket_id in ("laptop", "desktop"):
        await store.socket_opened(user_id, socket_id)
        await store.heartbeat(user_id, socket_id, visible=True)

    assert await store.socket_closed(user_id, "laptop") is False

    assert await redis.zrange(visible_key(user_id), 0, -1) == ["desktop"]


async def test_disconnect_takes_the_socket_out_of_the_visible_tabs(start_gateway, redis, user_id):
    gateway = start_gateway()
    tab = FakeSocket()
    await gateway.connect(user_id, tab, [])
    await gateway.heartbeat(user_id, tab, visible=True)
    assert await redis.exists(visible_key(user_id)) == 1

    await gateway.disconnect(user_id, tab)

    assert await redis.exists(visible_key(user_id)) == 0


async def test_an_expired_user_is_left_out_of_the_list_but_stays_in_the_set(
    redis, clean_keys, team_id, user_id
):
    store = PresenceStore(redis)
    crashed = uuid.uuid4()
    await store.socket_opened(user_id, "socket")
    await redis.sadd(online_key(team_id), str(user_id), str(crashed))

    assert await store.list_online(team_id) == [user_id]
    assert await redis.sismember(online_key(team_id), str(crashed))


async def test_go_offline_publishes_only_once(redis, clean_keys, team_id, user_id):
    store = PresenceStore(redis)
    await redis.sadd(online_key(team_id), str(user_id))
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(presence_channel(team_id))
        await pubsub.get_message(timeout=1)  # the subscribe confirmation

        await store.go_offline(user_id, [team_id])
        await store.go_offline(user_id, [team_id])

        first = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        second = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
    assert json.loads(first["data"])["status"] == "offline"
    assert second is None
