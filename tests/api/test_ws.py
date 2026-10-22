import threading
import uuid

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.deps import get_connections, get_core_client
from app.services.connection_registry import ConnectionRegistry
from tests.fakes import FakeCore, memberships

USER_ID = uuid.uuid4()
URL = "/api/chat/v1/ws"


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


@pytest.fixture
def ws_client(app, registry) -> TestClient:
    return TestClient(app)


def use_core(app, core: FakeCore) -> None:
    app.dependency_overrides[get_core_client] = lambda: core


def close_code(client: TestClient, url: str) -> int:
    with client.websocket_connect(url) as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_text()
    return closed.value.code


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
