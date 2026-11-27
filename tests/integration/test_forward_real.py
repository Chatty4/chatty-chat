import uuid
from typing import cast

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import undefer

from app.clients.core import CoreClient
from app.core.config import get_settings
from app.models import Attachment, ChannelCounter, ChannelEvent, Message, ReadState
from app.repositories import (
    AttachmentRepository,
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    PinRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.messages import MessageResponse, SendMessageRequest
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from tests.fakes import FakeBus, FakeCore, attached_file, channel, memberships

pytestmark = pytest.mark.integration

USER_ID = uuid.uuid4()
USER = memberships(USER_ID)


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def rooms(sessions):
    """Two channels of the same team, and their rows deleted afterwards."""
    room = channel(USER_ID)
    source_room = room.model_copy(update={"channel_id": uuid.uuid4()})
    yield room, source_room
    ids = (room.channel_id, source_room.channel_id)
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id.in_(ids)))


def service(session: AsyncSession, core: FakeCore) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        ReactionRepository(session),
        PinRepository(session),
        AccessService(cast(CoreClient, core)),
        cast(CoreClient, core),
        cast(EventBus, FakeBus()),
    )


async def send(sessions, core: FakeCore, channel_id: uuid.UUID, **fields) -> MessageResponse:
    data = SendMessageRequest(client_msg_id=uuid.uuid4(), **fields)
    async with sessions() as session:
        message, _ = await service(session, core).send(USER, channel_id, data)
    return message


async def test_a_forward_gets_copies_of_the_files_with_their_labels(sessions, rooms):
    room, source_room = rooms
    report, copy = attached_file(), attached_file()
    core = FakeCore(channel=room, channels=[source_room], attachments=[report], copies=[copy])
    source = await send(
        sessions, core, source_room.channel_id, body="q3", file_ids=[report.file_id]
    )
    async with sessions.begin() as session:
        await AttachmentRepository(session).save_labels(report.file_id, "pdf report", "revenue")

    forwarded = await send(sessions, core, room.channel_id, forward_of=source.id)

    assert (forwarded.forwarded, forwarded.body) == (True, "q3")
    assert forwarded.file_ids == [copy.file_id]
    async with sessions() as session:
        row = await session.scalar(
            select(Attachment)
            .options(undefer(Attachment.content))
            .where(Attachment.file_id == copy.file_id)
        )
    assert row is not None
    assert (row.labels, row.content) == ("pdf report", "revenue")
    assert row.labeled_at is not None


async def test_deleting_the_original_keeps_the_forward(sessions, rooms):
    room, source_room = rooms
    core = FakeCore(channel=room, channels=[source_room])
    source = await send(sessions, core, source_room.channel_id, body="hi")
    forwarded = await send(sessions, core, room.channel_id, forward_of=source.id)

    async with sessions() as session:
        await service(session, core).delete(USER, source.id)
        history = await service(session, core).list_history(USER, room.channel_id, None, 50)

    [kept] = history.messages
    assert (kept.id, kept.body, kept.forwarded, kept.deleted_at) == (forwarded.id, "hi", True, None)


async def test_deleting_the_forward_keeps_the_original(sessions, rooms):
    room, source_room = rooms
    core = FakeCore(channel=room, channels=[source_room])
    source = await send(sessions, core, source_room.channel_id, body="hi")
    forwarded = await send(sessions, core, room.channel_id, forward_of=source.id)

    async with sessions() as session:
        await service(session, core).delete(USER, forwarded.id)
        history = await service(session, core).list_history(USER, source_room.channel_id, None, 50)

    [original] = history.messages
    assert (original.id, original.body, original.deleted_at) == (source.id, "hi", None)
