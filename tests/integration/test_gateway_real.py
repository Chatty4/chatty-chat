import asyncio
import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from app.clients.core_events import CORE_EVENTS
from app.core.config import get_settings
from app.schemas.events import ChannelEvent
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import channel_name
from app.services.gateway import Gateway
from tests.fakes import FakeSocket

pytestmark = pytest.mark.integration


async def wait_until(check) -> None:
    async with asyncio.timeout(5):
        while not await check():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def subscribers(redis: Redis, channel_id: uuid.UUID) -> int:
    [(_, count)] = await redis.pubsub_numsub(channel_name(channel_id))
    return count


def core_event(event_type: str, **data) -> str:
    envelope = {"type": event_type, "occurred_at": "2026-10-27T12:00:00Z", "data": data}
    return json.dumps(envelope, default=str)


async def test_core_and_channel_events_reach_a_connected_socket():
    redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    gateway = Gateway(redis, ConnectionRegistry())
    task = asyncio.create_task(gateway.run())
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    try:
        await gateway.connect(user_id, socket, [channel_id])
        # pub/sub keeps nothing: publish only once Redis has the subscription
        await wait_until(lambda: _is(subscribers(redis, channel_id), 1))

        event = ChannelEvent(
            type="message.created",
            channel_id=channel_id,
            seq=1,
            created_at=datetime.now(UTC),
            payload={},
        )
        await redis.publish(channel_name(channel_id), event.model_dump_json())
        await wait_until(lambda: _has(socket, 1))
        assert socket.frames[0] == {"type": "event", "event": event.model_dump(mode="json")}

        await redis.publish(
            CORE_EVENTS,
            core_event(
                "channel.updated",
                channel_id=channel_id,
                team_id=uuid.uuid4(),
                name="releases",
                topic=None,
                archived=False,
            ),
        )
        await wait_until(lambda: _has(socket, 2))
        assert socket.frames[1]["type"] == "channel.updated"

        await redis.publish(
            CORE_EVENTS,
            core_event(
                "membership.changed",
                user_id=user_id,
                channel_id=channel_id,
                team_id=uuid.uuid4(),
                action="removed",
            ),
        )
        await wait_until(lambda: _is(subscribers(redis, channel_id), 0))
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await redis.aclose()


async def _is(count, expected: int) -> bool:
    return await count == expected


async def _has(socket: FakeSocket, frames: int) -> bool:
    return len(socket.frames) >= frames
