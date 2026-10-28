import uuid
from typing import cast

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.models import ChannelCounter, ChannelEvent, Message, ReadState
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel
from app.schemas.messages import EditMessageRequest, MessageResponse, SendMessageRequest
from app.schemas.sync import ChannelSyncResult
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from app.services.sync_service import SyncService
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


def access(room: CoreChannel) -> AccessService:
    return AccessService(cast(CoreClient, FakeCore(channel=room)))


def messages(session: AsyncSession, room: CoreChannel) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        access(room),
        cast(EventBus, FakeBus()),
    )


def sync(session: AsyncSession, room: CoreChannel) -> SyncService:
    return SyncService(
        session, ChannelEventRepository(session), ChannelCounterRepository(session), access(room)
    )


async def send(sessions, room: CoreChannel, body: str) -> MessageResponse:
    async with sessions() as session:
        message, _ = await messages(session, room).send(
            USER, room.channel_id, SendMessageRequest(client_msg_id=uuid.uuid4(), body=body)
        )
    return message


async def test_sync_and_events_return_every_event_after_the_cursor(sessions, room):
    first = await send(sessions, room, "a")  # seq 1
    await send(sessions, room, "b")  # seq 2
    async with sessions() as session:
        await messages(session, room).edit(USER, first.id, EditMessageRequest(body="a2"))  # seq 3

    async with sessions() as session:
        synced = (await sync(session, room).sync(USER, {room.channel_id: 1})).channels
    async with sessions() as session:
        page = await sync(session, room).list_channel_events(USER, room.channel_id, 1, 1)

    found = synced[room.channel_id]
    assert isinstance(found, ChannelSyncResult)
    assert [(e.seq, e.type) for e in found.events] == [
        (2, "message.created"),
        (3, "message.edited"),
    ]
    assert (found.last_seq, found.gap_too_large) == (3, False)
    assert ([e.seq for e in page.events], page.has_more) == ([2], True)


async def test_cleaned_up_events_are_gap_too_large(sessions, room):
    for body in ("a", "b", "c"):
        await send(sessions, room, body)
    async with sessions.begin() as session:  # what the nightly cleanup (CHAT-191) will do
        await session.execute(
            delete(ChannelEvent).where(
                ChannelEvent.channel_id == room.channel_id, ChannelEvent.seq <= 2
            )
        )

    async with sessions() as session:
        found = (await sync(session, room).sync(USER, {room.channel_id: 0})).channels
    async with sessions() as session:
        with pytest.raises(AppError) as error:
            await sync(session, room).list_channel_events(USER, room.channel_id, 0, 500)

    result = found[room.channel_id]
    assert isinstance(result, ChannelSyncResult)
    assert (result.events, result.gap_too_large) == ([], True)
    assert error.value.code == ErrorCode.GAP_TOO_LARGE
