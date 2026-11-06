import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress

import pytest
from redis.asyncio import Redis

from app.clients.presence import PresenceStore
from app.core.config import get_settings
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import EventBus, typing_channel
from app.services.gateway import Gateway
from tests.fakes import FakeSocket

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> AsyncGenerator[Redis]:
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def start_gateway(redis) -> AsyncGenerator[Callable[[], Gateway]]:
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


async def wait_for_subscribers(redis: Redis, channel_id: uuid.UUID, count: int) -> None:
    async with asyncio.timeout(5):
        while (await redis.pubsub_numsub(typing_channel(channel_id)))[0][1] != count:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def wait_for_frames(socket: FakeSocket, count: int) -> None:
    async with asyncio.timeout(5):
        while len(socket.frames) != count:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_typing_reaches_another_gateway_but_not_the_users_own_tabs(start_gateway, redis):
    gateway_a, gateway_b = start_gateway(), start_gateway()
    ana, bob, channel_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ana_tab, ana_other_tab, bob_tab = FakeSocket(), FakeSocket(), FakeSocket()
    await gateway_a.connect(ana, ana_tab, [channel_id])
    await gateway_b.connect(ana, ana_other_tab, [channel_id])
    await gateway_b.connect(bob, bob_tab, [channel_id])
    await wait_for_subscribers(redis, channel_id, 2)

    await EventBus(redis).send_typing(channel_id, ana)

    await wait_for_frames(bob_tab, 1)
    assert bob_tab.frames == [
        {"type": "typing", "channel_id": str(channel_id), "user_id": str(ana)}
    ]
    assert ana_tab.frames == []
    assert ana_other_tab.frames == []
    for gateway, user_id, socket in (
        (gateway_a, ana, ana_tab),
        (gateway_b, ana, ana_other_tab),
        (gateway_b, bob, bob_tab),
    ):
        await gateway.disconnect(user_id, socket)


async def test_a_gateway_stops_listening_with_the_channels_last_socket(start_gateway, redis):
    gateway = start_gateway()
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    first, second = FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first, [channel_id])
    await gateway.connect(user_id, second, [channel_id])
    await wait_for_subscribers(redis, channel_id, 1)

    await gateway.disconnect(user_id, first)
    await wait_for_subscribers(redis, channel_id, 1)

    await gateway.disconnect(user_id, second)
    await wait_for_subscribers(redis, channel_id, 0)
