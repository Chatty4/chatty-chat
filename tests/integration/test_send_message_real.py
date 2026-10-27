import asyncio
import uuid
from typing import cast

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.core.config import get_settings
from app.models import ChannelCounter, ChannelEvent, Message, ReadState
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel
from app.schemas.messages import SendMessageRequest
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from tests.fakes import FakeBus, FakeCore, channel, memberships

pytestmark = pytest.mark.integration

USER_ID = uuid.uuid4()
USER = memberships(USER_ID)


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def room(sessions):
    """A fresh channel id, and its rows deleted afterwards."""
    room = channel(USER_ID)
    yield room
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id == room.channel_id))


def service(session: AsyncSession, room: CoreChannel, bus: FakeBus) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        AccessService(cast(CoreClient, FakeCore(channel=room))),
        cast(EventBus, bus),
    )


async def send(sessions, room: CoreChannel, bus: FakeBus, data: SendMessageRequest):
    """One request: its own session, like get_session gives it."""
    async with sessions() as session:
        return await service(session, room, bus).send(USER, room.channel_id, data)


def request(client_msg_id: uuid.UUID | None = None) -> SendMessageRequest:
    return SendMessageRequest(client_msg_id=client_msg_id or uuid.uuid4(), body="hi")


async def count(sessions, model, channel_id: uuid.UUID) -> int:
    async with sessions() as session:
        stmt = select(func.count()).select_from(model).where(model.channel_id == channel_id)
        return await session.scalar(stmt)


async def test_messages_take_the_next_seq_and_are_stored(sessions, room):
    bus = FakeBus()

    first, _ = await send(sessions, room, bus, request())
    second, _ = await send(sessions, room, bus, request())

    assert (first.seq, second.seq) == (1, 2)
    async with sessions() as session:
        events = (
            await session.execute(
                select(ChannelEvent.seq, ChannelEvent.type, ChannelEvent.payload)
                .where(ChannelEvent.channel_id == room.channel_id)
                .order_by(ChannelEvent.seq)
            )
        ).all()
        last_seq = await session.scalar(
            select(ChannelCounter.last_seq).where(ChannelCounter.channel_id == room.channel_id)
        )
        last_read = await session.scalar(
            select(ReadState.last_read_seq).where(
                ReadState.user_id == USER_ID, ReadState.channel_id == room.channel_id
            )
        )
    assert [(seq, kind) for seq, kind, _ in events] == [
        (1, "message.created"),
        (2, "message.created"),
    ]
    assert events[0].payload["message"]["id"] == str(first.id)
    assert (last_seq, last_read) == (2, 2)
    assert [event["seq"] for _, event in bus.published] == [1, 2]


async def test_retry_returns_the_original_and_takes_no_seq(sessions, room):
    bus = FakeBus()
    data = request()

    original, created = await send(sessions, room, bus, data)
    retry, retried_created = await send(sessions, room, bus, data)

    assert (created, retried_created) == (True, False)
    assert retry == original
    assert await count(sessions, Message, room.channel_id) == 1
    assert await count(sessions, ChannelEvent, room.channel_id) == 1
    assert len(bus.published) == 1


async def test_concurrent_retries_save_one_message(sessions, room):
    bus = FakeBus()
    data = request()

    results = await asyncio.gather(send(sessions, room, bus, data), send(sessions, room, bus, data))

    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0].id == results[1][0].id
    assert await count(sessions, Message, room.channel_id) == 1
    assert await count(sessions, ChannelEvent, room.channel_id) == 1
    async with sessions() as session:
        last_seq = await session.scalar(
            select(ChannelCounter.last_seq).where(ChannelCounter.channel_id == room.channel_id)
        )
    assert last_seq == 1  # the loser's seq was rolled back, so no gap
    assert len(bus.published) == 1


async def test_read_position_only_moves_forward(sessions, room):
    async with sessions.begin() as session:
        await ReadStateRepository(session).advance_read_position(USER_ID, room.channel_id, 5)
    async with sessions.begin() as session:
        await ReadStateRepository(session).advance_read_position(USER_ID, room.channel_id, 3)

    async with sessions() as session:
        last_read = await session.scalar(
            select(ReadState.last_read_seq).where(
                ReadState.user_id == USER_ID, ReadState.channel_id == room.channel_id
            )
        )
    assert last_read == 5
