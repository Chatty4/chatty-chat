import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.deps import get_connections, get_core_client, get_event_bus
from app.api.routers import ws as ws_router
from app.api.routers.ws import handle_message
from app.schemas.core import Memberships
from app.schemas.events import ChannelEvent
from app.services.connection_registry import ConnectionRegistry
from tests.fakes import FakeCore, memberships

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
def registry(app) -> RecordingRegistry:
    registry = RecordingRegistry()
    app.dependency_overrides[get_connections] = lambda: registry
    return registry


class FakeBus:
    """Yields the given events, then stays subscribed like Redis does."""

    def __init__(self, *events: ChannelEvent) -> None:
        self.events = events
        self.calls: list[list[uuid.UUID]] = []

    def subscribe(self, channel_ids: list[uuid.UUID]):
        self.calls.append(channel_ids)
        return self._stream()

    async def _stream(self):
        for event in self.events:
            yield event
        await asyncio.Event().wait()


@pytest.fixture
def bus(app) -> FakeBus:
    # Without it get_event_bus reads app.state.redis, which TestClient never sets (no lifespan).
    bus = FakeBus()
    app.dependency_overrides[get_event_bus] = lambda: bus
    return bus


@pytest.fixture
def ws_client(app, registry, bus) -> TestClient:
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


def test_channel_events_are_forwarded_to_the_socket(app, ws_client, make_token):
    channel_id = uuid.uuid4()
    event = ChannelEvent(
        type="message.created",
        channel_id=channel_id,
        seq=1,
        created_at=datetime.now(UTC),
        payload={},
    )
    bus = FakeBus(event)
    app.dependency_overrides[get_event_bus] = lambda: bus
    use_core(app, FakeCore(memberships=member_of(channel_id)))
    token = make_token(sub=str(USER_ID))

    with ws_client.websocket_connect(f"{URL}?token={token}") as ws:
        assert ws.receive_json() == {"type": "event", "event": event.model_dump(mode="json")}

    assert bus.calls == [[channel_id]]


def test_user_without_channels_is_not_subscribed(app, ws_client, registry, bus, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    # subscribe() would run before connections.add(), so once added is set it never will
    with ws_client.websocket_connect(f"{URL}?token={token}"):
        assert registry.added.wait(timeout=2)

    assert bus.calls == []


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


def fake_socket() -> SimpleNamespace:
    """handle_message only touches websocket.state."""
    return SimpleNamespace(state=SimpleNamespace())


async def test_heartbeat_stores_visible_and_last_heartbeat_at():
    socket = fake_socket()
    before = asyncio.get_running_loop().time()

    assert await handle_message(socket, '{"type": "heartbeat", "visible": false}') is True

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
    ],
)
async def test_invalid_messages_are_ignored(raw):
    socket = fake_socket()

    assert await handle_message(socket, raw) is False

    assert vars(socket.state) == {}
