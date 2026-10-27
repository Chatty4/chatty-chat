import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from app.schemas.events import ChannelEvent
from app.services.event_bus import EventBus, channel_name


async def _aiter(items):
    for item in items:
        yield item


def make_pubsub(messages):
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    pubsub.listen.return_value = _aiter(messages)
    return pubsub


@pytest.fixture
def redis():
    mock = AsyncMock()
    mock.pubsub = MagicMock()
    return mock


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


async def test_subscribe_yields_parsed_envelope(bus, redis):
    cid = uuid.uuid4()
    raw = {
        "type": "message.created",
        "channel_id": str(cid),
        "seq": 1,
        "created_at": "2026-10-22T10:00:00Z",
        "payload": {"x": 1},
    }
    redis.pubsub.return_value = make_pubsub(
        [
            {"type": "subscribe", "channel": channel_name(cid), "data": 1},
            {"type": "message", "channel": channel_name(cid), "data": json.dumps(raw)},
        ]
    )

    results = [e async for e in bus.subscribe([cid])]

    assert len(results) == 1
    assert isinstance(results[0], ChannelEvent)
    assert results[0].type == "message.created"
    assert results[0].seq == 1


async def test_subscribe_ignores_unknown_fields(bus, redis):
    cid = uuid.uuid4()
    raw = {
        "type": "message.created",
        "channel_id": str(cid),
        "seq": 1,
        "created_at": "2026-10-22T10:00:00Z",
        "payload": {},
        "unknown_field": "ignored",
    }
    redis.pubsub.return_value = make_pubsub(
        [
            {"type": "message", "channel": channel_name(cid), "data": json.dumps(raw)},
        ]
    )

    results = [e async for e in bus.subscribe([cid])]
    assert len(results) == 1


async def test_subscribe_skips_malformed_message(bus, redis):
    cid = uuid.uuid4()
    valid = {
        "type": "message.created",
        "channel_id": str(cid),
        "seq": 2,
        "created_at": "2026-10-22T10:00:00Z",
        "payload": {},
    }
    redis.pubsub.return_value = make_pubsub(
        [
            {"type": "message", "channel": channel_name(cid), "data": "not-json{{{"},
            {"type": "message", "channel": channel_name(cid), "data": json.dumps(valid)},
        ]
    )

    results = [e async for e in bus.subscribe([cid])]
    assert len(results) == 1
    assert results[0].seq == 2


async def test_subscribe_skips_non_message_types(bus, redis):
    cid = uuid.uuid4()
    raw = {
        "type": "message.created",
        "channel_id": str(cid),
        "seq": 3,
        "created_at": "2026-10-22T10:00:00Z",
        "payload": {},
    }
    redis.pubsub.return_value = make_pubsub(
        [
            {"type": "subscribe", "channel": channel_name(cid), "data": 1},
            {"type": "unsubscribe", "channel": channel_name(cid), "data": 0},
            {"type": "message", "channel": channel_name(cid), "data": json.dumps(raw)},
        ]
    )

    results = [e async for e in bus.subscribe([cid])]
    assert len(results) == 1
    assert results[0].seq == 3


async def test_subscribe_cleans_up_on_exit(bus, redis):
    cid = uuid.uuid4()
    pubsub = make_pubsub([])
    redis.pubsub.return_value = pubsub

    async for _ in bus.subscribe([cid]):
        pass

    pubsub.unsubscribe.assert_called_once_with(channel_name(cid))
    pubsub.aclose.assert_called_once()
