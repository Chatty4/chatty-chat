import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_core_client, get_event_bus
from app.core.config import get_settings
from app.db.session import get_session
from app.models import ChannelCounter, ChannelEvent, Message, ReadState
from app.repositories import MessageRepository
from app.schemas.core import CoreChannel
from tests.fakes import FakeBus, FakeCore, channel, memberships

pytestmark = pytest.mark.integration

USER_ID = uuid.uuid4()
SENDS = 50


@pytest.fixture
async def sessions():
    # One connection per request so all 50 really run at once (the app's pool has 15).
    engine = create_async_engine(get_settings().DATABASE_URL, pool_size=SENDS, max_overflow=0)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def room(sessions):
    """A fresh channel, and its rows deleted afterwards."""
    room = channel(USER_ID)
    yield room
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id == room.channel_id))


@pytest.fixture
def bus(app, sessions, room) -> FakeBus:
    """The real routes, service and database; only chatty-core and Redis are faked."""
    bus = FakeBus()
    core = FakeCore(memberships=memberships(USER_ID), channel=room)

    async def session() -> AsyncGenerator[AsyncSession]:
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_event_bus] = lambda: bus
    return bus


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def send(
    client: AsyncClient, room: CoreChannel, auth: dict[str, str], client_msg_id: uuid.UUID
) -> Response:
    return await client.post(
        f"/api/chat/v1/channels/{room.channel_id}/messages",
        json={"client_msg_id": str(client_msg_id), "body": "hi"},
        headers=auth,
    )


async def stored(sessions, room: CoreChannel) -> tuple[int, list[int], int | None]:
    """(messages, channel_events seqs, the counter's last_seq) for the channel."""
    async with sessions() as session:
        messages = await session.scalar(
            select(func.count()).select_from(Message).where(Message.channel_id == room.channel_id)
        )
        event_seqs = await session.scalars(
            select(ChannelEvent.seq)
            .where(ChannelEvent.channel_id == room.channel_id)
            .order_by(ChannelEvent.seq)
        )
        last_seq = await session.scalar(
            select(ChannelCounter.last_seq).where(ChannelCounter.channel_id == room.channel_id)
        )
    return messages or 0, list(event_seqs), last_seq


async def test_duplicate_client_msg_id_is_idempotent(client, room, auth, bus, sessions):
    client_msg_id = uuid.uuid4()

    first = await send(client, room, auth, client_msg_id)
    retry = await send(client, room, auth, client_msg_id)

    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json() == first.json()  # same id, same seq: the original message
    assert await stored(sessions, room) == (1, [1], 1)  # no second message, no seq taken
    assert len(bus.published) == 1


async def test_seq_no_gaps_under_concurrent_sends(client, room, auth, bus, sessions):
    responses = await asyncio.gather(
        *(send(client, room, auth, uuid.uuid4()) for _ in range(SENDS))
    )

    assert [r.status_code for r in responses] == [201] * SENDS
    seqs = sorted(r.json()["seq"] for r in responses)
    assert seqs == list(range(1, SENDS + 1))  # no gaps, no duplicates
    assert len({r.json()["id"] for r in responses}) == SENDS
    assert await stored(sessions, room) == (SENDS, seqs, SENDS)
    assert sorted(event["seq"] for _, event in bus.published) == seqs


async def test_concurrent_duplicates_save_one_message(
    client, room, auth, bus, sessions, monkeypatch
):
    """Both requests pass the retry lookup before either saves, like a double-click."""
    original = MessageRepository.get_by_client_msg_id
    both_looked_up = asyncio.Barrier(2)
    lookups = 0

    async def lookup_then_wait(self, author_id, client_msg_id):
        nonlocal lookups
        found = await original(self, author_id, client_msg_id)
        lookups += 1
        if lookups <= 2:  # the loser looks up a third time after its insert fails
            await asyncio.wait_for(both_looked_up.wait(), 5)
        return found

    monkeypatch.setattr(MessageRepository, "get_by_client_msg_id", lookup_then_wait)
    client_msg_id = uuid.uuid4()

    responses = await asyncio.gather(
        send(client, room, auth, client_msg_id), send(client, room, auth, client_msg_id)
    )

    assert sorted(r.status_code for r in responses) == [200, 201]
    assert responses[0].json() == responses[1].json()
    assert await stored(sessions, room) == (1, [1], 1)  # the loser's seq was rolled back
    assert len(bus.published) == 1
