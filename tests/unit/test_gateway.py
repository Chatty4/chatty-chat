import asyncio
import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.clients.presence import PresenceStore, presence_channel
from app.schemas.core import Memberships, TeamMembership, TeamRole
from app.schemas.events import ChannelEvent
from app.services import gateway as gateway_module
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import channel_name, typing_channel, user_channel_name
from app.services.gateway import Gateway
from tests.fakes import FakePresence, FakeSocket, memberships


@pytest.fixture
def registry() -> ConnectionRegistry[FakeSocket]:
    return ConnectionRegistry()


@pytest.fixture
def presence() -> FakePresence:
    return FakePresence()


@pytest.fixture
def gateway(registry, presence) -> Gateway:
    # run() never starts, so the gateway has no pubsub and never touches Redis.
    return Gateway(MagicMock(), registry, cast(PresenceStore, presence))


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
    assert registry.users.members(user_id) == set()
    assert registry.channels.members(channel_id) == set()


async def test_user_without_channels_joins_none(gateway, registry):
    user_id = uuid.uuid4()
    socket = FakeSocket()

    await gateway.connect(user_id, socket, [])

    assert registry.users.members(user_id) == {socket}
    assert registry.channels.ids() == []


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
    assert registry.channels.members(channel_id) == set()


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

    assert registry.channels.members(channel_id) == {socket}
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

    assert registry.channels.members(channel_id) == {socket}
    assert socket.frames == []
    assert socket.closed is None


def viewer(*public_channel_ids: uuid.UUID, team_id: uuid.UUID | None = None) -> Memberships:
    """A user in one team whose public channels are public_channel_ids."""
    team = TeamMembership(
        team_id=team_id or uuid.uuid4(),
        role=TeamRole.MEMBER,
        public_channel_ids=list(public_channel_ids),
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

    assert registry.channels.members(channel_id) == set()
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
    assert registry.channels.members(first) == set()


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

    assert registry.channels.members(channel_id) == set()


async def test_unwatch_stops_the_events(gateway, registry):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    await gateway.unwatch(socket, channel_id)
    await deliver(gateway, channel_id)

    assert socket.frames == []
    assert registry.channels.members(channel_id) == set()


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

    assert registry.channels.members(channel_id) == {socket}


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
    assert registry.channels.members(channel_id) == set()


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
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
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
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [uuid.uuid4()])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert socket.closed is None


class StuckSocket(FakeSocket):
    """A client that stopped reading: send_json and close never finish (the client never
    answers the close handshake)."""

    async def send_json(self, data: Any) -> None:
        await asyncio.Event().wait()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)
        await asyncio.Event().wait()


async def test_a_stuck_socket_is_dropped_and_does_not_hold_up_the_others(
    gateway, registry, monkeypatch
):
    timeout = 0.3
    monkeypatch.setattr(gateway_module, "SEND_TIMEOUT_SECONDS", timeout)
    channel_id = uuid.uuid4()
    stuck, healthy = StuckSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), stuck, [channel_id])
    await gateway.connect(uuid.uuid4(), healthy, [channel_id])
    event = channel_event(channel_id)

    # One send timeout, not a send timeout plus a close timeout.
    async with asyncio.timeout(timeout * 1.5):
        await gateway.handle_channel_event(event.model_dump_json())
    await asyncio.sleep(0)  # lets the background close start

    assert healthy.frames == [event_frame(event)]
    assert stuck.closed == (1001, "too_slow")
    assert registry.channels.members(channel_id) == {healthy}
    await asyncio.gather(*gateway._closing)  # the close gives up after its own timeout


def presence_frame(team_id: uuid.UUID, user_id: uuid.UUID, status: str = "online") -> dict:
    return {
        "type": "presence",
        "team_id": str(team_id),
        "user_id": str(user_id),
        "status": status,
    }


async def test_the_first_socket_sends_online_to_each_team(gateway, presence):
    user_id, teams = uuid.uuid4(), [uuid.uuid4(), uuid.uuid4()]

    await gateway.connect(user_id, FakeSocket(), [], teams)

    assert presence.sent == [("online", user_id, teams)]


async def test_a_second_socket_sends_nothing(gateway, presence):
    user_id, team_id = uuid.uuid4(), uuid.uuid4()
    await gateway.connect(user_id, FakeSocket(), [], [team_id])
    presence.sent.clear()

    await gateway.connect(user_id, FakeSocket(), [], [team_id])

    assert presence.sent == []


async def test_only_the_last_socket_sends_offline(gateway, presence):
    user_id, team_id = uuid.uuid4(), uuid.uuid4()
    first, second = FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first, [], [team_id])
    await gateway.connect(user_id, second, [], [team_id])
    presence.sent.clear()

    await gateway.disconnect(user_id, first)
    assert presence.sent == []

    await gateway.disconnect(user_id, second)
    assert presence.sent == [("offline", user_id, [team_id])]


async def test_disconnect_leaves_the_teams(gateway, registry):
    user_id, team_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [], [team_id])

    await gateway.disconnect(user_id, socket)

    assert registry.teams.members(team_id) == set()


async def test_disconnect_of_a_socket_that_never_got_a_presence_id_sends_no_offline(
    gateway, registry, presence
):
    # connect() failed after joining the user, before it gave the socket its presence id
    user_id, socket = uuid.uuid4(), FakeSocket()
    registry.users.join(socket, user_id)

    await gateway.disconnect(user_id, socket)  # doesn't raise KeyError

    assert registry.users.members(user_id) == set()
    assert presence.sent == []


async def test_heartbeat_refreshes_the_line_of_the_socket(gateway, presence):
    user_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [])

    await gateway.heartbeat(user_id, socket)

    [line] = presence.lines[user_id]
    assert presence.heartbeats == [(user_id, line)]
    assert presence.sent == [("online", user_id, [])]  # only from the connect


async def test_presence_reaches_only_the_sockets_of_the_team(gateway):
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    inside, outside = FakeSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), inside, [], [team_id])
    await gateway.connect(uuid.uuid4(), outside, [], [uuid.uuid4()])

    await gateway.handle_presence(json.dumps(presence_frame(team_id, user_id, "offline")))

    assert inside.frames == [presence_frame(team_id, user_id, "offline")]
    assert outside.frames == []


async def test_malformed_presence_is_ignored(gateway):
    team_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [], [team_id])

    await gateway.handle_presence('{"type": "presence", "team_id": "nope"}')

    assert socket.frames == []


async def test_joining_a_team_while_connected_starts_presence_there(gateway, registry, presence):
    user_id, team_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [])
    presence.sent.clear()

    await gateway.handle_core_event(team_member_changed(user_id, team_id, "joined"))

    assert registry.teams.members(team_id) == {socket}
    assert presence.sent == [("online", user_id, [team_id])]


@pytest.mark.parametrize("action", ["left", "removed"])
async def test_leaving_a_team_stops_presence_there(gateway, registry, presence, action):
    user_id, team_id, other_team = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [], [team_id, other_team])
    presence.sent.clear()

    await gateway.handle_core_event(team_member_changed(user_id, team_id, action))

    assert registry.teams.members(team_id) == set()
    assert registry.teams.members(other_team) == {socket}
    assert presence.sent == [("offline", user_id, [team_id])]

    await gateway.disconnect(user_id, socket)  # the left team gets no second offline
    assert presence.sent[-1] == ("offline", user_id, [other_team])


async def test_a_role_change_leaves_presence_alone(gateway, registry, presence):
    user_id, team_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [], [team_id])
    presence.sent.clear()

    await gateway.handle_core_event(team_member_changed(user_id, team_id, "role_changed"))

    assert registry.teams.members(team_id) == {socket}
    assert presence.sent == []


async def test_a_team_change_of_a_user_without_sockets_here_does_nothing(gateway, presence):
    await gateway.handle_core_event(team_member_changed(uuid.uuid4(), uuid.uuid4(), "joined"))
    await gateway.handle_core_event(team_member_changed(uuid.uuid4(), uuid.uuid4(), "left"))

    assert presence.sent == []


async def test_a_too_slow_socket_gets_no_more_presence_but_still_goes_offline(
    gateway, presence, monkeypatch
):
    timeout = 0.2
    monkeypatch.setattr(gateway_module, "SEND_TIMEOUT_SECONDS", timeout)
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    stuck, healthy = StuckSocket(), FakeSocket()
    await gateway.connect(user_id, stuck, [], [team_id])
    await gateway.connect(uuid.uuid4(), healthy, [], [team_id])
    frame = json.dumps(presence_frame(team_id, uuid.uuid4()))

    await gateway.handle_presence(frame)  # finds out it is stuck, once

    async with asyncio.timeout(timeout / 2):  # the second frame doesn't wait for it
        await gateway.handle_presence(frame)
    assert len(healthy.frames) == 2

    await gateway.disconnect(user_id, stuck)
    assert presence.sent[-1] == ("offline", user_id, [team_id])


class ScriptedPubSub(FakePubSub):
    """Records subscriptions, gives run() these messages, then stays up."""

    def __init__(self, *messages: dict) -> None:
        super().__init__(drops=False)
        self.messages = messages
        self.channels: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.channels.extend(channels)

    async def listen(self):
        for message in self.messages:
            yield message
        await asyncio.Event().wait()


async def test_run_sends_presence_messages_to_the_team(registry):
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    message = {
        "type": "message",
        "channel": presence_channel(team_id),
        "data": json.dumps(presence_frame(team_id, user_id)),
    }
    redis = MagicMock()
    redis.pubsub.return_value = ScriptedPubSub(message)
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [], [team_id])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert socket.frames == [presence_frame(team_id, user_id)]


async def test_run_skips_subscribe_confirmations(registry, caplog):
    confirmation = {"type": "subscribe", "channel": presence_channel(uuid.uuid4()), "data": 1}
    redis = MagicMock()
    redis.pubsub.return_value = ScriptedPubSub(confirmation)
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert "malformed" not in caplog.text


async def test_run_subscribes_to_the_teams_of_open_sockets(registry):
    team_id = uuid.uuid4()
    pubsub = ScriptedPubSub()
    redis = MagicMock()
    redis.pubsub.return_value = pubsub
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    await gateway.connect(uuid.uuid4(), FakeSocket(), [], [team_id])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert presence_channel(team_id) in pubsub.channels


# live messages for one user (chat.users:{user_id}, D-12)


def unread_frame(channel_id: uuid.UUID) -> dict:
    return {"type": "unread", "channel_id": str(channel_id), "unread": 0, "mentions": 0}


async def test_a_user_message_reaches_every_socket_of_that_user_only(gateway):
    me, someone_else = uuid.uuid4(), uuid.uuid4()
    laptop, phone, theirs = FakeSocket(), FakeSocket(), FakeSocket()
    await gateway.connect(me, laptop, [])
    await gateway.connect(me, phone, [])
    await gateway.connect(someone_else, theirs, [])
    frame = unread_frame(uuid.uuid4())

    await gateway.handle_user_message(user_channel_name(me), json.dumps(frame))

    assert laptop.frames == [frame]
    assert phone.frames == [frame]
    assert theirs.frames == []


@pytest.mark.parametrize(
    ("redis_channel", "raw"),
    [("chat.users:not-a-uuid", "{}"), (user_channel_name(uuid.uuid4()), "{not json")],
    ids=["bad user id", "bad json"],
)
async def test_malformed_user_message_is_ignored(gateway, redis_channel, raw, caplog):
    await gateway.handle_user_message(redis_channel, raw)

    assert "malformed user message" in caplog.text


class RecordingPubSub(ScriptedPubSub):
    """Also records unsubscribes."""

    def __init__(self, *messages: dict) -> None:
        super().__init__(*messages)
        self.left: list[str] = []

    async def unsubscribe(self, *channels: str) -> None:
        self.left.extend(channels)


async def test_the_user_channel_is_joined_by_the_first_socket_and_left_by_the_last(registry):
    user_id = uuid.uuid4()
    pubsub = RecordingPubSub()
    redis = MagicMock()
    redis.pubsub.return_value = pubsub
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    first, second = FakeSocket(), FakeSocket()

    await gateway.connect(user_id, first, [])
    await gateway.connect(user_id, second, [])
    joined = pubsub.channels.count(user_channel_name(user_id))
    await gateway.disconnect(user_id, first)
    left_after_first = list(pubsub.left)
    await gateway.disconnect(user_id, second)
    await stop(task)

    assert joined == 1
    assert left_after_first == []
    assert pubsub.left == [user_channel_name(user_id)]


async def test_run_subscribes_to_the_users_of_open_sockets(registry):
    user_id = uuid.uuid4()
    pubsub = ScriptedPubSub()
    redis = MagicMock()
    redis.pubsub.return_value = pubsub
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    await gateway.connect(user_id, FakeSocket(), [])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert user_channel_name(user_id) in pubsub.channels


async def test_run_sends_user_messages_to_that_user(registry):
    user_id = uuid.uuid4()
    frame = unread_frame(uuid.uuid4())
    message = {"type": "message", "channel": user_channel_name(user_id), "data": json.dumps(frame)}
    redis = MagicMock()
    redis.pubsub.return_value = ScriptedPubSub(message)
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert socket.frames == [frame]


def typing_frame(channel_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    return {"type": "typing", "channel_id": str(channel_id), "user_id": str(user_id)}


async def test_a_member_can_type(gateway):
    channel_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [channel_id])

    assert gateway.can_type(socket, channel_id) is True


async def test_a_socket_outside_the_channel_cant_type(gateway):
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [uuid.uuid4()])

    assert gateway.can_type(socket, uuid.uuid4()) is False


async def test_a_watcher_cant_type(gateway):
    channel_id = uuid.uuid4()
    user = viewer(channel_id)
    socket = FakeSocket()
    await gateway.connect(user.user_id, socket, [])
    await gateway.watch(socket, user, channel_id)

    assert gateway.can_type(socket, channel_id) is False


async def test_someone_removed_from_the_channel_cant_type(gateway):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [channel_id])

    await gateway.handle_core_event(membership_changed(user_id, channel_id, "removed"))

    assert gateway.can_type(socket, channel_id) is False


async def test_typing_is_limited_to_once_every_3_seconds(gateway, monkeypatch):
    monkeypatch.setattr(gateway_module, "TYPING_INTERVAL_SECONDS", 0.05)
    channel_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [channel_id])

    assert gateway.can_type(socket, channel_id) is True
    assert gateway.can_type(socket, channel_id) is False
    await asyncio.sleep(0.06)
    assert gateway.can_type(socket, channel_id) is True


async def test_the_typing_limit_is_per_channel_and_per_socket(gateway):
    user_id, first_channel, second_channel = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first_tab, second_tab = FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first_tab, [first_channel, second_channel])
    await gateway.connect(user_id, second_tab, [first_channel])
    gateway.can_type(first_tab, first_channel)

    assert gateway.can_type(first_tab, second_channel) is True
    assert gateway.can_type(second_tab, first_channel) is True


async def test_disconnect_forgets_the_typing_times(gateway):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(user_id, socket, [channel_id])
    gateway.can_type(socket, channel_id)

    await gateway.disconnect(user_id, socket)

    assert gateway._typing_at == {}


async def test_typing_reaches_the_channel_and_its_watchers_but_not_other_channels(gateway):
    channel_id, user_id = uuid.uuid4(), uuid.uuid4()
    watcher = viewer(channel_id)
    member, watching, outside = FakeSocket(), FakeSocket(), FakeSocket()
    await gateway.connect(uuid.uuid4(), member, [channel_id])
    await gateway.connect(watcher.user_id, watching, [])
    await gateway.watch(watching, watcher, channel_id)
    await gateway.connect(uuid.uuid4(), outside, [uuid.uuid4()])

    await gateway.handle_typing(json.dumps(typing_frame(channel_id, user_id)))

    assert member.frames == [typing_frame(channel_id, user_id)]
    assert watching.frames == [typing_frame(channel_id, user_id)]
    assert outside.frames == []


async def test_typing_skips_all_the_typing_users_tabs(gateway):
    channel_id, user_id = uuid.uuid4(), uuid.uuid4()
    first_tab, second_tab = FakeSocket(), FakeSocket()
    await gateway.connect(user_id, first_tab, [channel_id])
    await gateway.connect(user_id, second_tab, [channel_id])

    await gateway.handle_typing(json.dumps(typing_frame(channel_id, user_id)))

    assert first_tab.frames == []
    assert second_tab.frames == []


async def test_typing_doesnt_wait_on_a_too_slow_socket(gateway, monkeypatch):
    timeout = 0.2
    monkeypatch.setattr(gateway_module, "SEND_TIMEOUT_SECONDS", timeout)
    channel_id, new_channel, stuck_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stuck, healthy = StuckSocket(), FakeSocket()
    await gateway.connect(stuck_user, stuck, [channel_id])
    await gateway.connect(uuid.uuid4(), healthy, [new_channel])
    await gateway.handle_channel_event(channel_event(channel_id).model_dump_json())  # now too_slow
    # membership.changed adds every socket of the user, the stuck one too
    await gateway.handle_core_event(membership_changed(stuck_user, new_channel, "added"))

    async with asyncio.timeout(timeout / 2):
        await gateway.handle_typing(json.dumps(typing_frame(new_channel, uuid.uuid4())))

    assert len(healthy.frames) == 1


async def test_a_malformed_typing_frame_is_ignored(gateway):
    channel_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [channel_id])

    await gateway.handle_typing('{"type": "typing", "channel_id": "nope"}')

    assert socket.frames == []


async def test_run_sends_typing_messages_to_the_channel(registry):
    channel_id, user_id = uuid.uuid4(), uuid.uuid4()
    message = {
        "type": "message",
        "channel": typing_channel(channel_id),
        "data": json.dumps(typing_frame(channel_id, user_id)),
    }
    redis = MagicMock()
    redis.pubsub.return_value = ScriptedPubSub(message)
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    socket = FakeSocket()
    await gateway.connect(uuid.uuid4(), socket, [channel_id])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert socket.frames == [typing_frame(channel_id, user_id)]


async def test_run_subscribes_to_typing_of_open_channels(registry):
    channel_id = uuid.uuid4()
    pubsub = ScriptedPubSub()
    redis = MagicMock()
    redis.pubsub.return_value = pubsub
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    await gateway.connect(uuid.uuid4(), FakeSocket(), [channel_id])

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    await stop(task)

    assert typing_channel(channel_id) in pubsub.channels


async def test_events_and_typing_are_joined_and_left_together(registry):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    pubsub = RecordingPubSub()
    redis = MagicMock()
    redis.pubsub.return_value = pubsub
    gateway = Gateway(redis, registry, cast(PresenceStore, FakePresence()))
    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.05)
    first, second = FakeSocket(), FakeSocket()

    await gateway.connect(user_id, first, [channel_id])
    await gateway.connect(user_id, second, [channel_id])
    joined = pubsub.channels.count(typing_channel(channel_id))
    await gateway.disconnect(user_id, first)
    left_after_first = list(pubsub.left)
    await gateway.disconnect(user_id, second)
    await stop(task)

    assert joined == 1
    assert typing_channel(channel_id) not in left_after_first
    assert {channel_name(channel_id), typing_channel(channel_id)} <= set(pubsub.left)
