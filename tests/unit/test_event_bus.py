import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.schemas.events import ChannelEvent, EventType
from app.services.event_bus import EventBus, channel_name, typing_channel


@pytest.fixture
def redis():
    return AsyncMock()


@pytest.fixture
def bus(redis):
    return EventBus(redis)


def event(channel_id: uuid.UUID) -> ChannelEvent:
    return ChannelEvent(
        type=EventType.MESSAGE_CREATED,
        channel_id=channel_id,
        seq=1,
        created_at=datetime(2026, 10, 22, 10, tzinfo=UTC),
        payload={},
    )


async def test_publish_serialises_to_the_event_channel(bus, redis):
    cid = uuid.uuid4()

    await bus.publish(event(cid))

    [(name, data)] = [call.args for call in redis.publish.call_args_list]
    assert name == channel_name(cid)
    assert json.loads(data) == {
        "type": "message.created",
        "channel_id": str(cid),
        "seq": 1,
        "created_at": "2026-10-22T10:00:00Z",
        "payload": {},
    }


async def test_publish_does_not_raise_on_redis_error(bus, redis):
    redis.publish.side_effect = RedisError("down")
    await bus.publish(event(uuid.uuid4()))


async def test_send_typing_publishes_on_the_typing_channel(bus, redis):
    channel_id, user_id = uuid.uuid4(), uuid.uuid4()

    await bus.send_typing(channel_id, user_id)

    [(name, data)] = [call.args for call in redis.publish.call_args_list]
    assert name == typing_channel(channel_id)
    assert json.loads(data) == {
        "type": "typing",
        "channel_id": str(channel_id),
        "user_id": str(user_id),
    }


async def test_send_typing_does_not_raise_on_redis_error(bus, redis):
    redis.publish.side_effect = RedisError("down")
    await bus.send_typing(uuid.uuid4(), uuid.uuid4())
