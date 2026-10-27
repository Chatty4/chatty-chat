import json
import uuid
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.services.event_bus import EventBus, channel_name


@pytest.fixture
def redis():
    return AsyncMock()


@pytest.fixture
def bus(redis):
    return EventBus(redis)


async def test_publish_serialises_to_correct_channel(bus, redis):
    cid = uuid.uuid4()
    event = {
        "type": "message.created",
        "channel_id": str(cid),
        "seq": 1,
        "created_at": "2026-10-22T10:00:00Z",
        "payload": {},
    }

    await bus.publish(cid, event)

    redis.publish.assert_called_once_with(channel_name(cid), json.dumps(event))


async def test_publish_does_not_raise_on_redis_error(bus, redis):
    redis.publish.side_effect = RedisError("down")
    await bus.publish(uuid.uuid4(), {"type": "test"})
