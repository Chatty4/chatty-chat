import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.deps import get_core_client, get_event_bus, get_gateway
from app.api.routers import ws as ws_router
from app.api.routers.ws import handle_message
from app.clients.core import CoreClient
from app.clients.presence import PresenceStore
from app.core.exceptions import ServiceUnavailable
from app.schemas.core import Memberships, TeamMembership, TeamRole
from app.services.access_service import AccessService
from app.services.connection_registry import ConnectionRegistry, SocketGroups
from app.services.event_bus import EventBus
from app.services.gateway import Gateway
from tests.fakes import FakeCore, FakePresence, FakeSocket, channel, memberships

USER_ID = uuid.uuid4()
URL = "/api/chat/v1/ws"
HEARTBEAT = {"type": "heartbeat", "visible": True}


class RecordingGroups(SocketGroups):
    """Sets joined once any socket joins."""

    def __init__(self) -> None:
        super().__init__()
        self.joined = threading.Event()

    def join(self, socket, group_id) -> bool:
        first = super().join(socket, group_id)
        self.joined.set()
        return first


class RecordingRegistry(ConnectionRegistry):
    """Lets a test wait until the server has added the socket."""

    def __init__(self) -> None:
        super().__init__()
        self.users = RecordingGroups()
        self.added = self.users.joined


@pytest.fixture
def registry() -> RecordingRegistry:
    return RecordingRegistry()


@pytest.fixture
def presence() -> FakePresence:
    return FakePresence()


@pytest.fixture
def bus() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def gateway(app, registry, presence, bus) -> Gateway:
    # Without it get_gateway reads app.state.gateway, which TestClient never sets (no lifespan).
    # run() never starts either, so the gateway never touches Redis.
    gateway = Gateway(MagicMock(), registry, cast(PresenceStore, presence))
    app.dependency_overrides[get_gateway] = lambda: gateway
    app.dependency_overrides[get_event_bus] = lambda: bus  # no lifespan, so no Redis
    return gateway


@pytest.fixture
def ws_client(app, gateway) -> TestClient:
    return TestClient(app)


def member_of(channel_id: uuid.UUID) -> Memberships:
    return Memberships.model_validate(
        {
            "user_id": str(USER_ID),
            "is_active": True,
            "teams": [],
            "channels": [
                {
                    "channel_id": str(channel_id),
                    "team_id": str(uuid.uuid4()),
                    "kind": "public",
                    "archived": False,
                    "role": "member",
                    "notify_level": "all",
                    "muted": False,
                    "hidden": False,
                }
            ],
        }
    )


def use_core(app, core: FakeCore) -> None:
    app.dependency_overrides[get_core_client] = lambda: core


def close_code(client: TestClient, url: str) -> int:
    with client.websocket_connect(url) as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_text()
    return closed.value.code


@pytest.mark.usefixtures("make_token")  # swaps in the test public key; CI has no keys/ file
@pytest.mark.parametrize("query", ["", "?token=not-a-jwt"], ids=["no token", "bad token"])
def test_missing_or_bad_token_closes_with_4001(app, ws_client, query):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))

    assert close_code(ws_client, URL + query) == 4001


def test_inactive_user_closes_with_4003(app, ws_client, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID, is_active=False)))
    token = make_token(sub=str(USER_ID))

    assert close_code(ws_client, f"{URL}?token={token}") == 4003


def test_core_down_at_connect_closes_with_1011(app, ws_client, make_token):
    use_core(app, FakeCore(error=ServiceUnavailable()))
    token = make_token(sub=str(USER_ID))

    assert close_code(ws_client, f"{URL}?token={token}") == 1011


def test_a_binary_frame_is_ignored_and_the_socket_stays_open(app, ws_client, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}") as ws:
        ws.send_bytes(b"\x00")
        ws.send_json({"type": "watch", "channel_id": str(uuid.uuid4())})

        assert ws.receive_json()["code"] == "cannot_watch"


def test_socket_is_registered_while_open_and_removed_after(app, ws_client, registry, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}"):
        assert registry.added.wait(timeout=2)
        assert len(registry.users.members(USER_ID)) == 1

    assert registry.users.members(USER_ID) == set()


def connect_and_fail(client: TestClient, url: str) -> None:
    """Connects to a server whose gateway.connect() raises; the error reaches the test client."""
    with (
        pytest.raises(RuntimeError, match="connect failed"),
        client.websocket_connect(url) as ws,
    ):
        ws.receive_text()


def test_a_connect_that_fails_halfway_still_removes_the_socket(
    app, ws_client, gateway, registry, make_token, monkeypatch
):
    channel_id = uuid.uuid4()
    use_core(app, FakeCore(memberships=member_of(channel_id)))
    connect = gateway.connect

    async def connect_then_fail(user_id, socket, channel_ids, team_ids):
        await connect(user_id, socket, channel_ids, team_ids)
        raise RuntimeError("connect failed")

    monkeypatch.setattr(gateway, "connect", connect_then_fail)

    connect_and_fail(ws_client, f"{URL}?token={make_token(sub=str(USER_ID))}")

    assert registry.users.members(USER_ID) == set()
    assert registry.channels.members(channel_id) == set()


def test_a_connect_that_fails_before_the_presence_id_still_removes_the_socket(
    app, ws_client, gateway, registry, presence, make_token, monkeypatch
):
    # Fails after joining the user and channels, before connect() gives the socket its
    # presence id: disconnect() must clean up without a KeyError that hides this error.
    channel_id = uuid.uuid4()
    use_core(app, FakeCore(memberships=member_of(channel_id)))

    async def subscribe_teams_fails(team_ids):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(gateway, "_subscribe_teams", subscribe_teams_fails)

    connect_and_fail(ws_client, f"{URL}?token={make_token(sub=str(USER_ID))}")

    assert registry.users.members(USER_ID) == set()
    assert registry.channels.members(channel_id) == set()
    assert presence.sent == []  # it never went online, so no offline either


def test_socket_closes_with_4001_when_the_token_expires(app, ws_client, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID), exp=datetime.now(UTC) + timedelta(seconds=2))

    assert close_code(ws_client, f"{URL}?token={token}") == 4001


def test_socket_joins_its_channels_and_leaves_them_after(app, ws_client, registry, make_token):
    channel_id = uuid.uuid4()
    use_core(app, FakeCore(memberships=member_of(channel_id)))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}"):
        assert registry.added.wait(timeout=2)
        # connect() joins the user before the channels, so wait for the channel join too
        for _ in range(20):
            if registry.channels.members(channel_id):
                break
            time.sleep(0.05)
        assert registry.channels.members(channel_id) == registry.users.members(USER_ID)

    assert registry.channels.members(channel_id) == set()


def test_socket_closes_with_4002_without_heartbeats(app, ws_client, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 0.5)
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    assert close_code(ws_client, f"{URL}?token={token}") == 4002


def test_heartbeats_keep_the_socket_open(app, ws_client, registry, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 0.5)
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}") as ws:
        assert registry.added.wait(timeout=2)
        for _ in range(10):  # 1 s, twice the timeout
            ws.send_json(HEARTBEAT)
            time.sleep(0.1)
        # the server removes the socket in its finally, so a closed socket would be gone
        assert len(registry.users.members(USER_ID)) == 1


def test_token_expiring_first_still_closes_with_4001(app, ws_client, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 5)
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID), exp=datetime.now(UTC) + timedelta(seconds=2))

    assert close_code(ws_client, f"{URL}?token={token}") == 4001


def test_only_heartbeat_resets_the_timer(app, ws_client, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 0.5)
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    with (
        ws_client.websocket_connect(f"{URL}?token={token}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        for _ in range(10):  # ignored frames for 1 s: the socket isn't closed for them
            ws.send_text("not json")
            ws.send_json({"type": "typing", "channel_id": str(uuid.uuid4())})
            time.sleep(0.1)
        ws.receive_text()

    assert closed.value.code == 4002


def test_heartbeat_timeout_after_the_gateway_closed_the_socket_is_quiet(
    app, ws_client, gateway, make_token, monkeypatch
):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 0.5)
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    connect = gateway.connect

    async def connect_then_drop(user_id, socket, channel_ids, team_ids):  # like _send's too_slow
        await connect(user_id, socket, channel_ids, team_ids)
        await socket.close(1001, "too_slow")

    monkeypatch.setattr(gateway, "connect", connect_then_drop)
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        time.sleep(1)  # the heartbeat timeout fires while the client is still connected

    assert closed.value.code == 1001


class HeartbeatSocket(FakeSocket):
    """A FakeSocket with the state a heartbeat writes to. The gateway needs it hashable."""

    def __init__(self) -> None:
        super().__init__()
        self.state = SimpleNamespace()


def fake_socket() -> SimpleNamespace:
    """Only state, no send_json: a test using it fails if handle_message sends anything."""
    return SimpleNamespace(state=SimpleNamespace())


async def handle(
    socket: object,
    raw: str,
    gateway: Gateway,
    user: Memberships,
    core: FakeCore | None = None,
    bus: AsyncMock | None = None,
) -> bool:
    """handle_message with a fake socket, which only duck-types a WebSocket."""
    access = AccessService(cast(CoreClient, core or FakeCore()))
    event_bus = cast(EventBus, bus or AsyncMock())
    return await handle_message(cast(WebSocket, socket), raw, gateway, access, event_bus, user)


def watcher_of(*public_channel_ids: uuid.UUID) -> Memberships:
    """USER_ID in one team whose public channels are public_channel_ids."""
    team = TeamMembership(
        team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=list(public_channel_ids)
    )
    return memberships(USER_ID, teams=[team])


CANNOT_WATCH = {
    "type": "error",
    "code": "cannot_watch",
    "message": "channel is not public in your teams",
}


async def test_heartbeat_stores_visible_and_last_heartbeat_at(gateway):
    socket = HeartbeatSocket()
    await gateway.connect(USER_ID, socket, [])
    before = asyncio.get_running_loop().time()
    raw = '{"type": "heartbeat", "visible": false}'

    assert await handle(socket, raw, gateway, memberships(USER_ID)) is True

    assert socket.state.visible is False
    assert socket.state.last_heartbeat_at >= before


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '"heartbeat"',
        "[1, 2]",
        "null",
        '{"type": "typing", "channel_id": "42aa"}',
        '{"type": "heartbeat", "visible": "yes"}',
        '{"type": "heartbeat", "visible": 1}',
        '{"type": "heartbeat"}',
        '{"type": "watch", "channel_id": "not-a-uuid"}',
        '{"type": "unwatch"}',
    ],
    ids=[
        "invalid json",
        "string",
        "array",
        "null",
        "unknown type",
        "string visible",
        "int visible",
        "missing visible",
        "watch with bad channel_id",
        "unwatch without channel_id",
    ],
)
async def test_invalid_messages_are_ignored(gateway, registry, raw):
    socket = fake_socket()

    assert await handle(socket, raw, gateway, memberships(USER_ID)) is False

    assert vars(socket.state) == {}
    assert registry.channels.ids() == []


async def test_watch_of_a_channel_not_public_in_the_users_teams_replies_cannot_watch(
    gateway, registry
):
    # FakeSocket has no state, so this also fails if watch touched the heartbeat fields
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})

    assert await handle(socket, raw, gateway, watcher_of(uuid.uuid4())) is False

    assert socket.frames == [CANNOT_WATCH]
    assert socket.closed is None
    assert registry.channels.members(channel_id) == set()


async def test_watch_of_a_public_channel_subscribes_without_a_reply(gateway, registry):
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    user = watcher_of(channel_id)
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})

    assert await handle(socket, raw, gateway, user, FakeCore(memberships=user)) is False

    assert socket.frames == []
    assert registry.channels.members(channel_id) == {socket}


async def test_watch_after_being_removed_from_the_team_replies_cannot_watch(gateway, registry):
    # The memberships from connect still list the channel; the fresh ones don't.
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})
    core = FakeCore(memberships=memberships(USER_ID))

    await handle(socket, raw, gateway, watcher_of(channel_id), core)

    assert socket.frames == [CANNOT_WATCH]
    assert registry.channels.members(channel_id) == set()


async def test_watch_in_a_team_joined_after_connect_subscribes(gateway, registry):
    # The memberships from connect don't have the team yet; the fresh ones do.
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})
    core = FakeCore(memberships=watcher_of(channel_id))

    await handle(socket, raw, gateway, memberships(USER_ID), core)

    assert socket.frames == []
    assert registry.channels.members(channel_id) == {socket}


async def test_watch_of_a_public_channel_newer_than_the_cache_subscribes(gateway, registry):
    # Created after members:{user_id} was cached, so public_channel_ids doesn't list it yet.
    new_public = channel(uuid.uuid4())
    team = TeamMembership(team_id=new_public.team_id, role=TeamRole.MEMBER, public_channel_ids=[])
    user = memberships(USER_ID, teams=[team])
    socket = FakeSocket()
    raw = json.dumps({"type": "watch", "channel_id": str(new_public.channel_id)})

    await handle(socket, raw, gateway, user, FakeCore(memberships=user, channel=new_public))

    assert socket.frames == []
    assert registry.channels.members(new_public.channel_id) == {socket}


@pytest.mark.parametrize(
    "core",
    [
        FakeCore(error=ServiceUnavailable()),
        FakeCore(memberships=memberships(USER_ID, is_active=False)),
    ],
    ids=["chatty-core down", "deactivated"],
)
async def test_watch_without_fresh_memberships_replies_cannot_watch(gateway, registry, core):
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})

    await handle(socket, raw, gateway, watcher_of(channel_id), core)

    assert socket.frames == [CANNOT_WATCH]
    assert socket.closed is None  # stays open; user.deactivated makes the gateway close it
    assert registry.channels.members(channel_id) == set()


async def test_unwatch_ends_the_watch_without_a_reply(gateway, registry):
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    user = watcher_of(channel_id)
    await gateway.watch(socket, user, channel_id)
    raw = json.dumps({"type": "unwatch", "channel_id": str(channel_id)})

    assert await handle(socket, raw, gateway, user) is False

    assert socket.frames == []
    assert registry.channels.members(channel_id) == set()


async def test_unwatch_without_a_watch_replies_nothing(gateway):
    socket = FakeSocket()
    raw = json.dumps({"type": "unwatch", "channel_id": str(uuid.uuid4())})

    assert await handle(socket, raw, gateway, memberships(USER_ID)) is False

    assert socket.frames == []


def test_watch_does_not_reset_the_heartbeat_timer(app, ws_client, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 0.5)
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))
    watch = {"type": "watch", "channel_id": str(uuid.uuid4())}

    with (
        ws_client.websocket_connect(f"{URL}?token={token}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        for _ in range(10):  # 1 s of watch messages, twice the timeout
            ws.send_json(watch)
            time.sleep(0.1)
        while True:  # skip the cannot_watch replies until the close
            ws.receive_json()

    assert closed.value.code == 4002


def test_a_socket_goes_online_in_the_users_teams_and_offline_after(
    app, ws_client, presence, make_token
):
    user = watcher_of()
    team_id = user.teams[0].team_id
    use_core(app, FakeCore(memberships=user))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}"):
        pass

    assert presence.sent == [("online", USER_ID, [team_id]), ("offline", USER_ID, [team_id])]


def typing_message(channel_id: uuid.UUID) -> str:
    return json.dumps({"type": "typing", "channel_id": str(channel_id)})


async def test_typing_of_a_member_is_sent_with_the_id_from_the_socket(gateway, bus):
    room = channel(USER_ID)
    socket = FakeSocket()
    await gateway.connect(USER_ID, socket, [room.channel_id])
    # a user_id from the client must not win over the one from the token
    raw = json.dumps(
        {"type": "typing", "channel_id": str(room.channel_id), "user_id": str(uuid.uuid4())}
    )

    assert (
        await handle(socket, raw, gateway, memberships(USER_ID), FakeCore(channel=room), bus=bus)
        is False
    )

    bus.send_typing.assert_awaited_once_with(room.channel_id, USER_ID)
    assert socket.frames == []


@pytest.mark.parametrize(
    "core",
    [
        FakeCore(channel=channel(USER_ID, archived=True)),
        FakeCore(channel=channel(USER_ID, team_deleted=True)),
        FakeCore(channel=channel(uuid.uuid4())),
        FakeCore(),
        FakeCore(error=ServiceUnavailable()),
    ],
    ids=["archived", "team deleted", "not a member any more", "no channel", "core down"],
)
async def test_typing_the_user_cant_write_is_ignored(gateway, bus, core):
    channel_id = uuid.uuid4()
    socket = FakeSocket()
    await gateway.connect(USER_ID, socket, [channel_id])

    await handle(socket, typing_message(channel_id), gateway, memberships(USER_ID), core, bus=bus)

    bus.send_typing.assert_not_called()
    assert socket.frames == []
    assert socket.closed is None


async def test_typing_too_soon_is_ignored_before_chatty_core_is_asked(gateway, bus):
    room = channel(USER_ID)
    socket = FakeSocket()
    await gateway.connect(USER_ID, socket, [room.channel_id])
    user, raw = memberships(USER_ID), typing_message(room.channel_id)
    await handle(socket, raw, gateway, user, FakeCore(channel=room), bus=bus)

    # handle_typing only catches AppError, so asking chatty-core now fails the test
    await handle(socket, raw, gateway, user, FakeCore(error=AssertionError("asked core")), bus=bus)

    bus.send_typing.assert_awaited_once()


async def test_typing_in_a_random_channel_doesnt_reach_chatty_core(gateway, bus):
    socket = FakeSocket()
    await gateway.connect(USER_ID, socket, [])
    core = FakeCore(error=AssertionError("asked core"))

    await handle(socket, typing_message(uuid.uuid4()), gateway, memberships(USER_ID), core, bus=bus)

    bus.send_typing.assert_not_called()


async def test_a_bad_typing_message_is_ignored(gateway, bus):
    socket = FakeSocket()
    await gateway.connect(USER_ID, socket, [])
    raw = '{"type": "typing", "channel_id": "nope"}'

    assert await handle(socket, raw, gateway, memberships(USER_ID), bus=bus) is False

    bus.send_typing.assert_not_called()


def test_typing_on_a_real_socket_reaches_the_bus(app, ws_client, bus, make_token):
    room = channel(USER_ID)
    use_core(app, FakeCore(memberships=member_of(room.channel_id), channel=room))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}") as ws:
        ws.send_text(typing_message(room.channel_id))

    bus.send_typing.assert_awaited_once_with(room.channel_id, USER_ID)


def test_typing_does_not_reset_the_heartbeat_timer(app, ws_client, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 0.5)
    room = channel(USER_ID)
    use_core(app, FakeCore(memberships=member_of(room.channel_id), channel=room))
    token = make_token(sub=str(USER_ID))

    with (
        ws_client.websocket_connect(f"{URL}?token={token}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        for _ in range(10):  # 1 s of typing, twice the timeout
            ws.send_text(typing_message(room.channel_id))
            time.sleep(0.1)
        ws.receive_text()

    assert closed.value.code == 4002
