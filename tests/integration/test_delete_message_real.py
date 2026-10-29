import asyncio
import uuid
from typing import cast

import pytest
from sqlalchemy import delete, select
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
from app.schemas.messages import EditMessageRequest, MessageResponse, SendMessageRequest
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from tests.fakes import FakeBus, FakeCore, channel, memberships

pytestmark = pytest.mark.integration

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def room(sessions):
    """A fresh channel id, and its rows deleted afterwards."""
    room = channel(USER_ID, OTHER_ID)
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


async def send(sessions, room: CoreChannel) -> MessageResponse:
    data = SendMessageRequest(client_msg_id=uuid.uuid4(), body=f"hi <@{OTHER_ID}>")
    async with sessions() as session:
        message, _ = await service(session, room, FakeBus()).send(USER, room.channel_id, data)
    return message


async def delete_message(sessions, room: CoreChannel, bus: FakeBus, message_id: uuid.UUID):
    async with sessions() as session:
        await service(session, room, bus).delete(USER, message_id)


async def edit_message(sessions, room: CoreChannel, message_id: uuid.UUID, body: str) -> None:
    async with sessions() as session:
        await service(session, room, FakeBus()).edit(
            USER, message_id, EditMessageRequest(body=body)
        )


async def event_rows(sessions, channel_id: uuid.UUID):
    async with sessions() as session:
        return (
            await session.execute(
                select(ChannelEvent.seq, ChannelEvent.type, ChannelEvent.payload)
                .where(ChannelEvent.channel_id == channel_id)
                .order_by(ChannelEvent.seq)
            )
        ).all()


async def last_seq(sessions, channel_id: uuid.UUID) -> int | None:
    async with sessions() as session:
        return await session.scalar(
            select(ChannelCounter.last_seq).where(ChannelCounter.channel_id == channel_id)
        )


async def test_delete_clears_the_row_and_takes_the_next_seq(sessions, room):
    sent = await send(sessions, room)
    assert sent.mentions == [OTHER_ID]
    bus = FakeBus()

    await delete_message(sessions, room, bus, sent.id)

    async with sessions() as session:
        row = await session.get(Message, sent.id)
    assert row is not None
    assert (row.body, row.mentions, row.seq) == ("", [], 1)  # keeps its place in the channel
    assert row.deleted_at is not None
    events = await event_rows(sessions, room.channel_id)
    assert [(seq, kind) for seq, kind, _ in events] == [
        (1, "message.created"),
        (2, "message.deleted"),
    ]
    assert events[1].payload["message_id"] == str(sent.id)
    assert events[1].payload["deleted_by"] == str(USER_ID)
    assert [event["seq"] for _, event in bus.published] == [2]


async def test_delete_clears_the_text_in_earlier_events(sessions, room):
    kept = await send(sessions, room)
    sent = await send(sessions, room)
    await edit_message(sessions, room, sent.id, f"secret <@{OTHER_ID}>")

    await delete_message(sessions, room, FakeBus(), sent.id)

    events = await event_rows(sessions, room.channel_id)  # a new session: what is stored
    assert [kind for _, kind, _ in events] == [
        "message.created",
        "message.created",
        "message.edited",
        "message.deleted",
    ]
    kept_created, created, edited, deleted = (payload for _, _, payload in events)
    assert (created["message"]["body"], created["message"]["mentions"]) == ("", [])
    assert (edited["body"], edited["mentions"]) == ("", [])
    assert "body" not in deleted  # message.deleted is left as it was
    assert kept_created["message"]["id"] == str(kept.id)
    assert kept_created["message"]["body"] == f"hi <@{OTHER_ID}>"  # other messages keep theirs


async def test_second_delete_is_a_no_op(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    await delete_message(sessions, room, bus, sent.id)
    await delete_message(sessions, room, bus, sent.id)

    assert await last_seq(sessions, room.channel_id) == 2
    assert len(await event_rows(sessions, room.channel_id)) == 2
    assert len(bus.published) == 1


async def test_concurrent_deletes_write_one_event(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    await asyncio.gather(
        delete_message(sessions, room, bus, sent.id),
        delete_message(sessions, room, bus, sent.id),
    )

    assert await last_seq(sessions, room.channel_id) == 2  # the loser's seq was rolled back
    assert [kind for _, kind, _ in await event_rows(sessions, room.channel_id)] == [
        "message.created",
        "message.deleted",
    ]
    assert len(bus.published) == 1
