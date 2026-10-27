import asyncio
import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.schemas.core import Memberships, TeamMembership
from app.schemas.events import ChannelEvent
from app.services import gateway as gateway_module
from app.services.connection_registry import ConnectionRegistry
from app.services.gateway import Gateway
from tests.fakes import FakeSocket, memberships


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


def viewer(*public_channel_ids: uuid.UUID, team_id: uuid.UUID | None = None) -> Memberships:
    """A user in one team whose public channels are public_channel_ids."""
    team = TeamMembership(
        team_id=team_id or uuid.uuid4(), role="member", public_channel_ids=list(public_channel_ids)
    )
    return memberships(uuid.uuid4(), teams=[team])


async def deliver(gateway: Gateway, channel_id: uuid.UUID) -> dict:
    """Sends one channel event through the gateway and returns the frame a socket should get."""
    event = channel_event(channel_id)
    await gateway.handle_channel_event(event.model_dump_json())
    return event_frame(event)


async def test_watch_of_a_public_channel_delivers_its_events(gateway):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])

    assert await gateway.watch(socket, user, channel_id) is True
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


async def test_watch_of_a_channel_not_public_in_the_users_teams_is_refused(gateway, registry):
    channel_id = uuid.uuid4()
    user = viewer(uuid.uuid4())  # public channels of the team don't include channel_id
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])

    assert await gateway.watch(socket, user, channel_id) is False
    await deliver(gateway, channel_id)

    assert registry.in_channel(channel_id) == set()
    assert socket.frames == []


async def test_a_new_watch_replaces_the_old_one(gateway, registry):
    first, second = uuid.uuid4(), uuid.uuid4()
    user = viewer(first, second)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])

    await gateway.watch(socket, user, first)
    await gateway.watch(socket, user, second)
    await deliver(gateway, first)
    frame = await deliver(gateway, second)

    assert socket.frames == [frame]
    assert registry.in_channel(first) == set()


async def test_replacing_a_watch_keeps_other_watchers_of_the_old_channel(gateway):
    old, new = uuid.uuid4(), uuid.uuid4()
    user = viewer(old, new)
    switching, staying = FakeSocket(), FakeSocket()
    await gateway.connect(user.user_id, switching, [])
    await gateway.connect(user.user_id, staying, [])
    await gateway.watch(switching, user, old)
    await gateway.watch(staying, user, old)

    await gateway.watch(switching, user, new)
    frame = await deliver(gateway, old)

    assert staying.frames == [frame]
    assert switching.frames == []


async def test_watching_the_same_channel_twice_is_one_watch(gateway, registry):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])

    assert await gateway.watch(socket, user, channel_id) is True
    assert await gateway.watch(socket, user, channel_id) is True
    await gateway.unwatch(socket, channel_id)

    assert registry.in_channel(channel_id) == set()


async def test_unwatch_stops_the_events(gateway, registry):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.unwatch(socket, channel_id)
    await deliver(gateway, channel_id)

    assert socket.frames == []
    assert registry.in_channel(channel_id) == set()


async def test_unwatch_of_another_channel_keeps_the_watch(gateway):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.unwatch(socket, uuid.uuid4())
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


async def test_unwatch_without_a_watch_does_nothing(gateway, registry):
    channel_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [channel_id])

    await gateway.unwatch(socket, channel_id)

    assert registry.in_channel(channel_id) == {socket}


async def test_watch_and_unwatch_of_a_joined_channel_keep_the_membership(gateway):
    # public_channel_ids lists joined channels too; unwatch must not cut the membership
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [channel_id])

    assert await gateway.watch(socket, user, channel_id) is True
    await gateway.unwatch(socket, channel_id)
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


async def test_disconnect_forgets_the_watch(gateway):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.disconnect(user.user_id, socket)
    await gateway.connect(user.user_id, socket, [])
    # with a stale watch left behind, this would return early as "already watching"
    await gateway.watch(socket, user, channel_id)
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


def team_member_changed(user_id: uuid.UUID, team_id: uuid.UUID, action: str) -> str:
    role = "member" if action in ("joined", "role_changed") else None
    return core_event(
        "team.member.changed", user_id=user_id, team_id=team_id, action=action, role=role
    )


async def test_joining_the_watched_channel_turns_the_watch_into_a_membership(gateway):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.handle_core_event(membership_changed(user.user_id, channel_id, "added"))
    # without the promotion, this unwatch would leave a channel the user is now a member of
    await gateway.unwatch(socket, channel_id)
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


async def test_joining_another_channel_keeps_the_watch(gateway):
    watched, joined = uuid.uuid4(), uuid.uuid4()
    user = viewer(watched)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, watched)

    await gateway.handle_core_event(membership_changed(user.user_id, joined, "added"))
    await gateway.unwatch(socket, watched)
    await deliver(gateway, watched)

    assert socket.frames == []  # still a watch, so unwatch ended it


@pytest.mark.parametrize("action", ["left", "removed"])
async def test_leaving_the_team_ends_its_watch_on_every_tab(gateway, registry, action):
    team_id, channel_id = uuid.uuid4(), uuid.uuid4()
    user = viewer(channel_id, team_id=team_id)
    first_tab, second_tab = FakeSocket(), FakeSocket()
    for socket in (first_tab, second_tab):
        await gateway.connect(user.user_id, socket, [])
        await gateway.watch(socket, user, channel_id)

    await gateway.handle_core_event(team_member_changed(user.user_id, team_id, action))
    await deliver(gateway, channel_id)

    assert first_tab.frames == second_tab.frames == []
    assert registry.in_channel(channel_id) == set()


@pytest.mark.parametrize("action", ["joined", "role_changed"])
async def test_other_team_changes_keep_the_watch(gateway, action):
    team_id, channel_id = uuid.uuid4(), uuid.uuid4()
    user = viewer(channel_id, team_id=team_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.handle_core_event(team_member_changed(user.user_id, team_id, action))
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


async def test_leaving_another_team_keeps_the_watch(gateway):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.handle_core_event(team_member_changed(user.user_id, uuid.uuid4(), "left"))
    frame = await deliver(gateway, channel_id)

    assert socket.frames == [frame]


async def test_another_user_leaving_the_team_keeps_my_watch(gateway):
    team_id, channel_id = uuid.uuid4(), uuid.uuid4()
    me, other = viewer(channel_id, team_id=team_id), viewer(channel_id, team_id=team_id)
    mine, theirs = FakeSocket(), FakeSocket()
    await gateway.connect(me.user_id, mine, [])
    await gateway.connect(other.user_id, theirs, [])
    await gateway.watch(mine, me, channel_id)
    await gateway.watch(theirs, other, channel_id)

    await gateway.handle_core_event(team_member_changed(other.user_id, team_id, "removed"))
    frame = await deliver(gateway, channel_id)

    assert mine.frames == [frame]
    assert theirs.frames == []


class FakePubSub:
    """One Redis pub/sub connection for run(): it drops right away, or stays up."""

    def __init__(self, drops: bool) -> None:
        self._drops = drops

    async def __aenter__(self) -> FakePubSub:
        return self

    async def __aexit__(self, *exc) -> None:
        pass

    async def subscribe(self, *channels: str) -> None:
        pass

    async def listen(self):
        if self._drops:
            raise RedisConnectionError("Redis went away")
        await asyncio.Event().wait()  # stays up until the test cancels run()
        yield


class ClosingSocket(FakeSocket):
    """Lets a test wait until the gateway closes the socket."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_event = asyncio.Event()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await super().close(code, reason)
        self.closed_event.set()


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_after_a_redis_drop_every_socket_is_closed_with_1001(registry):
    redis = MagicMock()
    redis.pubsub.side_effect = [FakePubSub(drops=True), FakePubSub(drops=False)]
    gateway = Gateway(redis, registry)
    socket = ClosingSocket()
    await gateway.connect(uuid.uuid4(), socket, [uuid.uuid4()])

    task = asyncio.create_task(gateway.run())
    async with asyncio.timeout(3):  # run() waits 1 s before it reconnects
        await socket.closed_event.wait()
    await stop(task)

    assert socket.closed == (1001, "gateway_restart")


async def test_the_first_connect_keeps_the_sockets_open(registry):
    redis = MagicMock()
    redis.pubsub.return_value = FakePubSub(drops=False)
    gateway = Gateway(redis, registry)
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [uuid.uuid4()])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert socket.closed is None


class StuckSocket(FakeSocket):
    """A client that stopped reading: send_json never returns."""

    async def send_json(self, data: Any) -> None:
        await asyncio.Event().wait()


async def test_a_stuck_socket_is_dropped_and_does_not_hold_up_the_others(
    gateway, registry, monkeypatch
):
    monkeypatch.setattr(gateway_module, "SEND_TIMEOUT_SECONDS", 0.05)
    channel_id = uuid.uuid4()
    stuck, healthy = StuckSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), stuck, [channel_id])
    await gateway.connect(uuid.uuid4(), healthy, [channel_id])
    event = channel_event(channel_id)

    async with asyncio.timeout(1):
        await gateway.handle_channel_event(event.model_dump_json())

    assert healthy.frames == [event_frame(event)]
    assert stuck.closed == (1001, "too_slow")
    assert registry.in_channel(channel_id) == {healthy}
