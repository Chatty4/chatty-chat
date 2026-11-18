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
    PinRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel, TeamMembership, TeamRole
from app.schemas.messages import EditMessageRequest, MessageResponse, SendMessageRequest
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from app.services.pin_service import PinService
from app.services.search_service import SearchService
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
async def room(sessions):
    """A fresh channel, and its rows deleted afterwards (pins and files go with their message)."""
    room = channel(USER_ID)
    yield room
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id == room.channel_id))


def access(room: CoreChannel) -> AccessService:
    return AccessService(cast(CoreClient, FakeCore(channel=room)))


def message_service(session: AsyncSession, room: CoreChannel) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        ReactionRepository(session),
        PinRepository(session),
        access(room),
        cast(CoreClient, FakeCore()),
        cast(EventBus, FakeBus()),
    )


async def send(sessions, room: CoreChannel, body: str = "hi") -> MessageResponse:
    data = SendMessageRequest(client_msg_id=uuid.uuid4(), body=body)
    async with sessions() as session:
        message, _ = await message_service(session, room).send(USER, room.channel_id, data)
    return message


async def add_files(sessions, message_id: uuid.UUID, *names: str) -> list[uuid.UUID]:
    """Stored as send will store them (CHAT-212 step 5): position is the order of the list."""
    files = [attached_file(name) for name in names]
    async with sessions.begin() as session:
        await MessageRepository(session).insert_attachments(message_id, files)
    return [file.file_id for file in files]


@pytest.mark.parametrize(
    "page",
    [{}, {"around_seq": 2}, {"after_seq": 0}],
    ids=["before_seq", "around_seq", "after_seq"],
)
async def test_history_pages_have_the_files_in_order(sessions, room, page):
    first = await send(sessions, room)
    with_files = await send(sessions, room)
    await send(sessions, room)
    file_ids = await add_files(sessions, with_files.id, "report.pdf", "chart.png", "notes.txt")

    async with sessions() as session:
        history = await message_service(session, room).list_history(
            USER, room.channel_id, None, 10, **page
        )

    found = {message.id: message for message in history.messages}
    assert found[with_files.id].file_ids == file_ids
    assert [item.filename for item in found[with_files.id].attachments] == [
        "report.pdf",
        "chart.png",
        "notes.txt",
    ]
    assert found[first.id].file_ids == [] and found[first.id].attachments == []


async def test_deleted_message_has_no_files(sessions, room):
    message = await send(sessions, room)
    await add_files(sessions, message.id, "report.pdf")

    async with sessions() as session:
        await message_service(session, room).delete(USER, message.id)
    async with sessions() as session:
        history = await message_service(session, room).list_history(USER, room.channel_id, None, 10)

    assert history.messages[0].file_ids == [] and history.messages[0].attachments == []


async def test_edit_returns_the_files(sessions, room):
    message = await send(sessions, room)
    file_ids = await add_files(sessions, message.id, "report.pdf", "chart.png")

    async with sessions() as session:
        edited = await message_service(session, room).edit(
            USER, message.id, EditMessageRequest(body="edited")
        )

    assert edited.file_ids == file_ids


async def test_pins_have_the_files(sessions, room):
    message = await send(sessions, room)
    file_ids = await add_files(sessions, message.id, "report.pdf", "chart.png")

    async with sessions() as session:
        service = PinService(
            session,
            MessageRepository(session),
            PinRepository(session),
            ReactionRepository(session),
            ChannelCounterRepository(session),
            ChannelEventRepository(session),
            access(room),
            cast(EventBus, FakeBus()),
        )
        await service.pin(USER, room.channel_id, message.id)
        pins = await service.list_pins(USER, room.channel_id)

    assert pins.pins[0].message.file_ids == file_ids


async def test_search_results_have_the_files(sessions, room):
    message = await send(sessions, room, "quarterly report attached")
    file_ids = await add_files(sessions, message.id, "report.pdf")
    team = TeamMembership(
        team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[room.channel_id]
    )

    async with sessions() as session:
        service = SearchService(
            MessageRepository(session),
            ReactionRepository(session),
            PinRepository(session),
            access(room),
        )
        page = await service.search(
            memberships(USER_ID, teams=[team]),
            room.team_id,
            "quarterly",
            None,
            None,
            None,
            None,
            None,
            20,
        )

    assert [result.message.file_ids for result in page.results] == [file_ids]
