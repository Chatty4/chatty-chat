import uuid
from typing import cast

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.core.config import get_settings
from app.core.exceptions import Conflict, ValidationFailed
from app.models import ChannelCounter, ChannelEvent, Message, ReadState
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    PinRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel
from app.schemas.messages import (
    MessageHistoryResponse,
    MessageResponse,
    ReplyPreview,
    SendMessageRequest,
)
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
    """A fresh channel, and its rows deleted afterwards."""
    room = channel(USER_ID)
    yield room
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id == room.channel_id))


@pytest.fixture
async def other_room(sessions):
    """A second channel of the same user, with its rows deleted afterwards."""
    other = channel(USER_ID)
    yield other
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id == other.channel_id))


def service(session: AsyncSession, room: CoreChannel) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        ReactionRepository(session),
        PinRepository(session),
        AccessService(cast(CoreClient, FakeCore(channel=room))),
        cast(CoreClient, FakeCore()),
        cast(EventBus, FakeBus()),
    )


async def send(
    sessions, room: CoreChannel, body: str, reply_to: uuid.UUID | None = None
) -> MessageResponse:
    data = SendMessageRequest(client_msg_id=uuid.uuid4(), body=body, reply_to_id=reply_to)
    async with sessions() as session:
        message, _ = await service(session, room).send(USER, room.channel_id, data)
    return message


async def history(sessions, room: CoreChannel) -> MessageHistoryResponse:
    async with sessions() as session:
        return await service(session, room).list_history(USER, room.channel_id, None, 50)


async def delete_message(sessions, room: CoreChannel, message_id: uuid.UUID) -> None:
    async with sessions() as session:
        await service(session, room).delete(USER, message_id)


async def test_a_reply_quotes_the_replied_message(sessions, room):
    replied = await send(sessions, room, "is v1.4 ready?")

    reply = await send(sessions, room, "yes", reply_to=replied.id)

    quote = ReplyPreview(
        id=replied.id, seq=replied.seq, author_id=USER_ID, body="is v1.4 ready?", deleted=False
    )
    assert reply.reply_to == quote
    newest, oldest = (await history(sessions, room)).messages
    assert (newest.id, newest.reply_to) == (reply.id, quote)
    assert oldest.reply_to is None


async def test_a_reply_to_a_message_of_another_channel_is_refused(sessions, room, other_room):
    elsewhere = await send(sessions, other_room, "hi")

    with pytest.raises(ValidationFailed):
        await send(sessions, room, "yes", reply_to=elsewhere.id)

    assert (await history(sessions, room)).messages == []


async def test_a_reply_to_a_deleted_message_is_refused(sessions, room):
    replied = await send(sessions, room, "hi")
    await delete_message(sessions, room, replied.id)

    with pytest.raises(Conflict):
        await send(sessions, room, "yes", reply_to=replied.id)
