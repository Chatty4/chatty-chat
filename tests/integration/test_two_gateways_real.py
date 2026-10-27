import asyncio
import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.deps import get_core_client
from app.api.routers import ws as ws_router
from app.clients.core_events import CORE_EVENTS
from app.core.config import get_settings
from app.main import create_app
from app.models import ChannelCounter, Message, ReadState
from app.models import ChannelEvent as ChannelEventRow
from app.schemas.core import CoreChannel, Memberships
from app.services.event_bus import channel_name
from tests.fakes import FakeCore, channel

pytestmark = pytest.mark.integration

ANA, BOB = uuid.uuid4(), uuid.uuid4()
WS_URL = "/api/chat/v1/ws"


def member_of(user_id: uuid.UUID, room: CoreChannel) -> Memberships:
    return Memberships.model_validate(
        {
            "user_id": str(user_id),
            "is_active": True,
            "teams": [],
            "channels": [
                {
                    "channel_id": str(room.channel_id),
                    "team_id": str(room.team_id),
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


async def delete_rows(channel_id: uuid.UUID) -> None:
    engine = create_async_engine(get_settings().DATABASE_URL)
    async with engine.begin() as conn:
        for model in (ChannelEventRow, Message, ChannelCounter, ReadState):
            await conn.execute(delete(model).where(model.channel_id == channel_id))
    await engine.dispose()


@pytest.fixture
def room() -> Iterator[CoreChannel]:
    """A fresh channel with Ana and Bob, and its rows deleted afterwards."""
    room = channel(ANA, BOB)
    yield room
    asyncio.run(delete_rows(room.channel_id))


@pytest.fixture
def redis() -> Iterator[Redis]:
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    yield client
    client.close()


StartGateway = Callable[[uuid.UUID, CoreChannel], TestClient]


@pytest.fixture
def start_gateway(make_token, monkeypatch) -> Iterator[StartGateway]:
    """Starts a whole chatty-chat app with its lifespan. Each one is its own gateway."""
    # A lost event closes the socket with 4002 after 5 s instead of hanging receive_json forever.
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT_SECONDS", 5)
    with ExitStack() as gateways:

        def start(user_id: uuid.UUID, room: CoreChannel) -> TestClient:
            app = create_app()
            core = FakeCore(memberships=member_of(user_id, room), channel=room)
            app.dependency_overrides[get_core_client] = lambda: core
            return gateways.enter_context(TestClient(app))

        yield start


def wait_for_subscribers(redis: Redis, channel_id: uuid.UUID, count: int) -> None:
    """Pub/sub keeps nothing: publish only once the gateways have subscribed."""
    deadline = time.monotonic() + 5
    while redis.pubsub_numsub(channel_name(channel_id))[0][1] < count:
        assert time.monotonic() < deadline, f"fewer than {count} gateways subscribed"
        time.sleep(0.05)


def test_a_message_sent_through_one_gateway_reaches_a_socket_on_the_other(
    start_gateway, room, redis, make_token
):
    gateway_1 = start_gateway(ANA, room)
    gateway_2 = start_gateway(BOB, room)

    with gateway_1.websocket_connect(f"{WS_URL}?token={make_token(sub=str(ANA))}") as ana:
        # Only gateway 1 has a socket in the channel, so the event can reach Ana only via Redis.
        wait_for_subscribers(redis, room.channel_id, 1)
        response = gateway_2.post(
            f"/api/chat/v1/channels/{room.channel_id}/messages",
            json={"client_msg_id": str(uuid.uuid4()), "body": "hi Ana"},
            headers={"Authorization": f"Bearer {make_token(sub=str(BOB))}"},
        )
        assert response.status_code == 201
        frame = ana.receive_json()

    assert frame["type"] == "event"
    assert frame["event"]["type"] == "message.created"
    assert frame["event"]["payload"]["message"]["id"] == response.json()["id"]


def test_channel_updated_reaches_sockets_on_both_gateways(start_gateway, room, redis, make_token):
    gateway_1 = start_gateway(ANA, room)
    gateway_2 = start_gateway(BOB, room)

    with (
        gateway_1.websocket_connect(f"{WS_URL}?token={make_token(sub=str(ANA))}") as ana,
        gateway_2.websocket_connect(f"{WS_URL}?token={make_token(sub=str(BOB))}") as bob,
    ):
        # A gateway subscribes channels only after core.events, so 2 here means both hear it.
        wait_for_subscribers(redis, room.channel_id, 2)
        update = {
            "channel_id": room.channel_id,
            "team_id": room.team_id,
            "name": "releases",
            "topic": None,
            "archived": False,
        }
        event = {"type": "channel.updated", "occurred_at": "2026-10-27T12:00:00Z", "data": update}
        redis.publish(CORE_EVENTS, json.dumps(event, default=str))
        frames = ana.receive_json(), bob.receive_json()

    expected = {
        "type": "channel.updated",
        "channel_id": str(room.channel_id),
        "name": "releases",
        "topic": None,
        "archived": False,
    }
    assert frames == (expected, expected)
