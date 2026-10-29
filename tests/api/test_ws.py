import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.deps import get_core_client, get_gateway
from app.api.routers import ws as ws_router
from app.api.routers.ws import handle_message
from app.core.exceptions import ServiceUnavailable
from app.schemas.core import Memberships, TeamMembership
from app.services.connection_registry import ConnectionRegistry
from app.services.gateway import Gateway
from tests.fakes import FakeCore, FakeSocket, memberships

USER_ID = uuid.uuid4()
URL = "/api/chat/v1/ws"
HEARTBEAT = {"type": "heartbeat", "visible": True}


class RecordingRegistry(ConnectionRegistry):
    """Lets a test wait until the server has added the socket."""

    def __init__(self) -> None:
        super().__init__()
        self.added = threading.Event()

    def add(self, user_id, socket) -> None:
        super().add(user_id, socket)
        self.added.set()


@pytest.fixture
def registry() -> RecordingRegistry:
    return RecordingRegistry()


@pytest.fixture
def gateway(app, registry) -> Gateway:
    # Without it get_gateway reads app.state.gateway, which TestClient never sets (no lifespan).
    # run() never starts either, so the gateway never touches Redis.
    gateway = Gateway(MagicMock(), registry)
    app.dependency_overrides[get_gateway] = lambda: gateway
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
        assert len(registry.sockets(USER_ID)) == 1

    assert registry.sockets(USER_ID) == set()


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
        # connect() runs add() before join(), so wait for the join too
        for _ in range(20):
            if registry.in_channel(channel_id):
                break
            time.sleep(0.05)
        assert registry.in_channel(channel_id) == registry.sockets(USER_ID)

    assert registry.in_channel(channel_id) == set()


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
        assert len(registry.sockets(USER_ID)) == 1


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

    async def connect_then_drop(user_id, socket, channel_ids):  # like _send's too_slow close
        await connect(user_id, socket, channel_ids)
        await socket.close(1001, "too_slow")

    monkeypatch.setattr(gateway, "connect", connect_then_drop)
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        time.sleep(1)  # the heartbeat timeout fires while the client is still connected

    assert closed.value.code == 1001


def fake_socket() -> SimpleNamespace:
    """Only state, no send_json: a test using it fails if handle_message sends anything."""
    return SimpleNamespace(state=SimpleNamespace())


def watcher_of(*public_channel_ids: uuid.UUID) -> Memberships:
    """USER_ID in one team whose public channels are public_channel_ids."""
    team = TeamMembership(
        team_id=uuid.uuid4(), role="member", public_channel_ids=list(public_channel_ids)
    )
    return memberships(USER_ID, teams=[team])


CANNOT_WATCH = {
    "type": "error",
    "code": "cannot_watch",
    "message": "channel is not public in your teams",
}


async def test_heartbeat_stores_visible_and_last_heartbeat_at(gateway):
    socket = fake_socket()
    before = asyncio.get_running_loop().time()
    raw = '{"type": "heartbeat", "visible": false}'

    assert await handle_message(socket, raw, gateway, memberships(USER_ID)) is True

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

    assert await handle_message(socket, raw, gateway, memberships(USER_ID)) is False

    assert vars(socket.state) == {}
    assert registry.channel_ids() == []


async def test_watch_of_a_channel_not_public_in_the_users_teams_replies_cannot_watch(
    gateway, registry
):
    # FakeSocket has no state, so this also fails if watch touched the heartbeat fields
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})

    assert await handle_message(socket, raw, gateway, watcher_of(uuid.uuid4())) is False

    assert socket.frames == [CANNOT_WATCH]
    assert socket.closed is None
    assert registry.in_channel(channel_id) == set()


async def test_watch_of_a_public_channel_subscribes_without_a_reply(gateway, registry):
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    raw = json.dumps({"type": "watch", "channel_id": str(channel_id)})

    assert await handle_message(socket, raw, gateway, watcher_of(channel_id)) is False

    assert socket.frames == []
    assert registry.in_channel(channel_id) == {socket}


async def test_unwatch_ends_the_watch_without_a_reply(gateway, registry):
    socket = FakeSocket()
    channel_id = uuid.uuid4()
    user = watcher_of(channel_id)
    await gateway.watch(socket, user, channel_id)
    raw = json.dumps({"type": "unwatch", "channel_id": str(channel_id)})

    assert await handle_message(socket, raw, gateway, user) is False

    assert socket.frames == []
    assert registry.in_channel(channel_id) == set()


async def test_unwatch_without_a_watch_replies_nothing(gateway):
    socket = FakeSocket()
    raw = json.dumps({"type": "unwatch", "channel_id": str(uuid.uuid4())})

    assert await handle_message(socket, raw, gateway, memberships(USER_ID)) is False

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
