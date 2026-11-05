import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.core.config import get_settings
from app.models import ChannelCounter, Message, ReadState
from app.repositories import ChannelCounterRepository, ReadStateRepository
from app.repositories.read_state import UNREAD_CAP, UnreadCounts
from app.schemas.core import CoreChannel
from app.schemas.read_state import MarkReadResponse
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.read_state_service import ReadStateService
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
async def make_room(sessions):
    """Fresh channels, with their rows deleted afterwards."""
    rooms: list[CoreChannel] = []

    def make() -> CoreChannel:
        room = channel(USER_ID, OTHER_ID)
        rooms.append(room)
        return room

    yield make
    async with sessions.begin() as session:
        for room in rooms:
            for model in (Message, ChannelCounter, ReadState):
                await session.execute(delete(model).where(model.channel_id == room.channel_id))


def msg(
    author: uuid.UUID = OTHER_ID, mentions: list[uuid.UUID] | None = None, deleted: bool = False
) -> dict:
    return {"author": author, "mentions": mentions or [], "deleted": deleted}


async def add_messages(sessions, room: CoreChannel, *messages: dict) -> None:
    """Saves the messages with seq 1, 2, 3, ... and the channel's counter."""
    async with sessions.begin() as session:
        for seq, fields in enumerate(messages, start=1):
            session.add(
                Message(
                    channel_id=room.channel_id,
                    seq=seq,
                    author_id=fields["author"],
                    client_msg_id=uuid.uuid4(),
                    body="hi",
                    mentions=fields["mentions"],
                    deleted_at=datetime.now(UTC) if fields["deleted"] else None,
                )
            )
        session.add(ChannelCounter(channel_id=room.channel_id, last_seq=len(messages)))


async def mark_read(sessions, room: CoreChannel, seq: int) -> MarkReadResponse:
    async with sessions() as session:
        service = ReadStateService(
            session,
            ReadStateRepository(session),
            ChannelCounterRepository(session),
            AccessService(cast(CoreClient, FakeCore(channel=room))),
            cast(EventBus, FakeBus()),
        )
        return await service.mark_read(USER, room.channel_id, seq)


async def count_unread(sessions, *rooms: CoreChannel) -> list[UnreadCounts]:
    async with sessions() as session:
        return await ReadStateRepository(session).count_unread(
            USER_ID, [room.channel_id for room in rooms]
        )


async def test_counts_skip_my_own_and_deleted_messages_and_count_mentions(sessions, make_room):
    room = make_room()
    await add_messages(
        sessions,
        room,
        msg(),  # 1: read below
        msg(),  # 2: unread
        msg(author=USER_ID),  # 3: mine, never unread
        msg(deleted=True),  # 4: deleted, never unread
        msg(mentions=[USER_ID]),  # 5: unread mention
        msg(mentions=[OTHER_ID]),  # 6: unread, mentions someone else
        msg(author=USER_ID, mentions=[USER_ID]),  # 7: mine, even if it mentions me
    )

    result = await mark_read(sessions, room, 1)

    assert (result.last_read_seq, result.unread, result.mentions) == (1, 3, 1)


async def test_a_channel_never_read_counts_from_the_start(sessions, make_room):
    room = make_room()
    await add_messages(sessions, room, msg(), msg(mentions=[USER_ID]))

    assert await count_unread(sessions, room) == [UnreadCounts(room.channel_id, 2, 1)]


async def test_every_channel_gets_a_row_even_with_nothing_unread(sessions, make_room):
    busy, empty, all_read = make_room(), make_room(), make_room()
    await add_messages(sessions, busy, msg(), msg())
    await add_messages(sessions, all_read, msg())
    await mark_read(sessions, all_read, 1)

    counts = await count_unread(sessions, busy, empty, all_read)

    assert sorted(counts) == sorted(
        [
            UnreadCounts(busy.channel_id, 2, 0),
            UnreadCounts(empty.channel_id, 0, 0),
            UnreadCounts(all_read.channel_id, 0, 0),
        ]
    )


async def test_counts_stop_at_the_cap(sessions, make_room):
    room = make_room()
    await add_messages(sessions, room, *[msg(mentions=[USER_ID]) for _ in range(UNREAD_CAP + 20)])

    assert await count_unread(sessions, room) == [
        UnreadCounts(room.channel_id, UNREAD_CAP, UNREAD_CAP)
    ]


async def test_the_position_only_moves_forward_and_stops_at_the_last_message(sessions, make_room):
    room = make_room()
    await add_messages(sessions, room, msg(), msg(), msg())

    lowered = await mark_read(sessions, room, 999)
    older = await mark_read(sessions, room, 1)  # an old tab

    assert (lowered.last_read_seq, lowered.unread) == (3, 0)
    assert (older.last_read_seq, older.unread) == (3, 0)


async def test_no_channels_is_an_empty_list(sessions):
    assert await count_unread(sessions) == []
