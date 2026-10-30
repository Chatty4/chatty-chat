import uuid
from typing import cast

import pytest
from sqlalchemy import delete, func, select, update
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
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel
from app.schemas.messages import EditMessageRequest, MessageResponse, SendMessageRequest
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
        ReactionRepository(session),
        AccessService(cast(CoreClient, FakeCore(channel=room))),
        cast(EventBus, bus),
    )


async def send(sessions, room: CoreChannel) -> MessageResponse:
    async with sessions() as session:
        message, _ = await service(session, room, FakeBus()).send(
            USER, room.channel_id, SendMessageRequest(client_msg_id=uuid.uuid4(), body="hi")
        )
    return message


async def edit(sessions, room: CoreChannel, bus: FakeBus, message_id: uuid.UUID, body: str):
    async with sessions() as session:
        return await service(session, room, bus).edit(
            USER, message_id, EditMessageRequest(body=body)
        )


async def last_seq(sessions, channel_id: uuid.UUID) -> int | None:
    async with sessions() as session:
        return await session.scalar(
            select(ChannelCounter.last_seq).where(ChannelCounter.channel_id == channel_id)
        )


async def test_edit_updates_the_row_and_takes_the_next_seq(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    edited = await edit(sessions, room, bus, sent.id, "changed")

    assert (edited.id, edited.seq, edited.body) == (sent.id, 1, "changed")
    assert edited.edited_at is not None
    async with sessions() as session:
        row = await session.get(Message, sent.id)
        events = (
            await session.execute(
                select(ChannelEvent.seq, ChannelEvent.type, ChannelEvent.payload)
                .where(ChannelEvent.channel_id == room.channel_id)
                .order_by(ChannelEvent.seq)
            )
        ).all()
    assert row is not None
    assert (row.body, row.seq, row.edited_at) == ("changed", 1, edited.edited_at)
    assert [(seq, kind) for seq, kind, _ in events] == [
        (1, "message.created"),
        (2, "message.edited"),
    ]
    assert events[1].payload["message_id"] == str(sent.id)
    assert events[1].payload["body"] == "changed"
    assert [event["seq"] for _, event in bus.published] == [2]


async def test_deleted_message_is_409_and_takes_no_seq(sessions, room):
    sent = await send(sessions, room)
    async with sessions.begin() as session:
        await session.execute(
            update(Message).where(Message.id == sent.id).values(deleted_at=func.now())
        )
    bus = FakeBus()

    with pytest.raises(AppError) as error:
        await edit(sessions, room, bus, sent.id, "changed")

    assert error.value.code == ErrorCode.MESSAGE_DELETED
    assert await last_seq(sessions, room.channel_id) == 1
    assert bus.published == []


async def test_update_skips_a_deleted_message(sessions, room):
    """The conditional UPDATE is what catches a delete between the checks and the write."""
    sent = await send(sessions, room)
    async with sessions.begin() as session:
        await session.execute(
            update(Message).where(Message.id == sent.id).values(deleted_at=func.now())
        )

    async with sessions.begin() as session:
        updated = await MessageRepository(session).update_message_body(sent.id, "changed", [])

    assert updated is None
    async with sessions() as session:
        row = await session.get(Message, sent.id)
    assert row is not None
    assert (row.body, row.edited_at) == ("hi", None)
