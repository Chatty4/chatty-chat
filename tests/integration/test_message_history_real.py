import uuid
from typing import cast

import pytest
from sqlalchemy import delete
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
from app.schemas.messages import MessageHistoryResponse, MessageResponse, SendMessageRequest
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


def service(session: AsyncSession, room: CoreChannel) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        AccessService(cast(CoreClient, FakeCore(channel=room))),
        cast(EventBus, FakeBus()),
    )


async def send(sessions, room: CoreChannel, body: str) -> MessageResponse:
    async with sessions() as session:
        message, _ = await service(session, room).send(
            USER, room.channel_id, SendMessageRequest(client_msg_id=uuid.uuid4(), body=body)
        )
    return message


async def history(
    sessions, room: CoreChannel, before_seq: int | None, limit: int
) -> MessageHistoryResponse:
    async with sessions() as session:
        return await service(session, room).list_history(USER, room.channel_id, before_seq, limit)


async def test_pages_through_the_history_with_before_seq(sessions, room):
    for body in ("m1", "m2", "m3"):
        await send(sessions, room, body)

    first = await history(sessions, room, None, 2)
    second = await history(sessions, room, first.messages[-1].seq, 2)

    assert [message.body for message in first.messages] == ["m3", "m2"]
    assert first.has_more is True
    assert [message.body for message in second.messages] == ["m1"]
    assert second.has_more is False
    assert first.last_seq == second.last_seq == 3


async def test_deleted_message_keeps_its_place_and_last_seq_counts_the_delete(sessions, room):
    first = await send(sessions, room, "a")
    await send(sessions, room, "b")
    async with sessions() as session:
        await service(session, room).delete(USER, first.id)

    page = await history(sessions, room, None, 50)

    assert [(m.seq, m.body, m.deleted_at is not None) for m in page.messages] == [
        (2, "b", False),
        (1, "", True),
    ]
    assert page.last_seq == 3  # the delete took seq 3


async def test_channel_without_messages_has_last_seq_0(sessions, room):
    page = await history(sessions, room, None, 50)

    assert (page.messages, page.has_more, page.last_seq) == ([], False, 0)
