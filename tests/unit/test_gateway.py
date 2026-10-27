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


def core_event(event_type: str, **data) -> str:
    """A core.events message as chatty-core publishes it (core-events.md)."""
    envelope = {"type": event_type, "occurred_at": "2026-10-27T12:00:00Z", "data": data}
    return json.dumps(envelope, default=str)  # default=str turns the UUIDs into strings


def membership_changed(user_id: uuid.UUID, channel_id: uuid.UUID, action: str) -> str:
    return core_event(
        "membership.changed",
        user_id=user_id,
        channel_id=channel_id,
        team_id=uuid.uuid4(),
        action=action,
    )


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


async def test_added_starts_delivering_the_channel_to_all_user_sockets(gateway):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    first_tab, second_tab = FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first_tab, [])
    await gateway.connect(user_id, second_tab, [])

    await gateway.handle_core_event(membership_changed(user_id, channel_id, "added"))
    event = channel_event(channel_id)
    await gateway.handle_channel_event(event.model_dump_json())

    # both tabs, not only the first: any() over a generator would stop after one join
    assert first_tab.frames == second_tab.frames == [event_frame(event)]


async def test_removed_stops_delivering_the_channel(gateway, registry):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [channel_id])

    await gateway.handle_core_event(membership_changed(user_id, channel_id, "removed"))
    await gateway.handle_channel_event(channel_event(channel_id).model_dump_json())

    assert socket.frames == []
    assert registry.in_channel(channel_id) == set()


async def test_removed_keeps_other_members_of_the_channel(gateway):
    removed_user, channel_id = uuid.uuid4(), uuid.uuid4()
    removed, staying = FakeSocket(), FakeSocket()
    await gateway.connect(removed_user, removed, [channel_id])
    await gateway.connect(uuid.uuid4(), staying, [channel_id])

    await gateway.handle_core_event(membership_changed(removed_user, channel_id, "removed"))
    event = channel_event(channel_id)
    await gateway.handle_channel_event(event.model_dump_json())

    assert removed.frames == []
    assert staying.frames == [event_frame(event)]


async def test_settings_changes_nothing(gateway, registry):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [channel_id])

    await gateway.handle_core_event(membership_changed(user_id, channel_id, "settings"))

    assert registry.in_channel(channel_id) == {socket}
    assert socket.frames == []


async def test_channel_updated_reaches_every_socket_in_the_channel(gateway):
    channel_id = uuid.uuid4()
    first, second, outside = FakeSocket(), FakeSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), first, [channel_id])
    await gateway.connect(uuid.uuid4(), second, [channel_id])
    await gateway.connect(uuid.uuid4(), outside, [uuid.uuid4()])

    await gateway.handle_core_event(
        core_event(
            "channel.updated",
            channel_id=channel_id,
            team_id=uuid.uuid4(),
            name="releases",
            topic=None,
            archived=True,
        )
    )

    frame = {
        "type": "channel.updated",
        "channel_id": str(channel_id),
        "name": "releases",
        "topic": None,
        "archived": True,
    }
    assert first.frames == second.frames == [frame]
    assert outside.frames == []


async def test_user_deactivated_closes_all_sockets_of_that_user(gateway):
    user_id = uuid.uuid4()
    first_tab, second_tab, other_user = FakeSocket(), FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first_tab, [])
    await gateway.connect(user_id, second_tab, [])
    await gateway.connect(uuid.uuid4(), other_user, [])

    await gateway.handle_core_event(core_event("user.deactivated", user_id=user_id))

    assert first_tab.closed == second_tab.closed == (4003, "user_inactive")
    assert other_user.closed is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        core_event("membership.changed", user_id=uuid.uuid4(), action="added"),
        membership_changed(uuid.uuid4(), uuid.uuid4(), "exploded"),
        core_event("team.deleted", team_id=uuid.uuid4()),
    ],
    ids=["invalid json", "missing channel_id", "unknown action", "type the gateway skips"],
)
async def test_ignored_core_events_change_nothing(gateway, registry, raw):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [channel_id])

    await gateway.handle_core_event(raw)

    assert registry.in_channel(channel_id) == {socket}
    assert socket.frames == []
    assert socket.closed is None
