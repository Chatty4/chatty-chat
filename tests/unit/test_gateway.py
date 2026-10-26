import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.schemas.events import ChannelEvent
from app.services.connection_registry import ConnectionRegistry
from app.services.gateway import Gateway
from tests.fakes import FakeSocket


@pytest.fixture
def registry() -> ConnectionRegistry[FakeSocket]:
    return ConnectionRegistry()


@pytest.fixture
def gateway(registry) -> Gateway:
    # run() never starts, so the gateway has no pubsub and never touches Redis.
    return Gateway(MagicMock(), registry)


def channel_event(channel_id: uuid.UUID) -> ChannelEvent:
    return ChannelEvent(
        type="message.created",
        channel_id=channel_id,
        seq=1,
        created_at=datetime.now(UTC),
        payload={},
    )


def event_frame(event: ChannelEvent) -> dict:
    return {"type": "event", "event": event.model_dump(mode="json")}


async def test_channel_event_reaches_only_the_sockets_in_that_channel(gateway):
    channel_id = uuid.uuid4()
    inside, outside = FakeSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), inside, [channel_id])
    await gateway.connect(uuid.uuid4(), outside, [uuid.uuid4()])
    event = channel_event(channel_id)

    await gateway.handle_channel_event(event.model_dump_json())

    assert inside.frames == [event_frame(event)]
    assert outside.frames == []


async def test_every_socket_in_the_channel_gets_the_event(gateway):
    channel_id = uuid.uuid4()
    first, second = FakeSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), first, [channel_id])
    await gateway.connect(uuid.uuid4(), second, [channel_id])
    event = channel_event(channel_id)

    await gateway.handle_channel_event(event.model_dump_json())

    assert first.frames == second.frames == [event_frame(event)]


async def test_a_failing_socket_does_not_stop_the_others(gateway):
    channel_id = uuid.uuid4()
    broken, healthy = FakeSocket(error=RuntimeError("closed")), FakeSocket()
    await gateway.connect(uuid.uuid4(), broken, [channel_id])
    await gateway.connect(uuid.uuid4(), healthy, [channel_id])
    event = channel_event(channel_id)

    await gateway.handle_channel_event(event.model_dump_json())

    assert healthy.frames == [event_frame(event)]


async def test_after_disconnect_the_socket_gets_no_more_events(gateway, registry):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [channel_id])

    await gateway.disconnect(user_id, socket)
    await gateway.handle_channel_event(channel_event(channel_id).model_dump_json())

    assert socket.frames == []
    assert registry.sockets(user_id) == set()
    assert registry.in_channel(channel_id) == set()


async def test_user_without_channels_joins_none(gateway, registry):
    user_id = uuid.uuid4()
    socket = FakeSocket()

    await gateway.connect(user_id, socket, [])

    assert registry.sockets(user_id) == {socket}
    assert registry.channel_ids() == []


@pytest.mark.parametrize(
    "raw",
    ["not json", json.dumps({"type": "message.created"})],
    ids=["invalid json", "missing fields"],
)
async def test_malformed_channel_event_is_ignored(gateway, raw):
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [uuid.uuid4()])

    await gateway.handle_channel_event(raw)

    assert socket.frames == []
