import uuid
from collections.abc import AsyncGenerator, Callable
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
from app.schemas.core import ChannelMembership, CoreChannel, Memberships, TeamMembership, TeamRole
from app.schemas.messages import EditMessageRequest, MessageResponse, SendMessageRequest
from app.schemas.read_state import ChannelUnread, MarkReadResponse
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from app.services.reaction_service import ReactionService
from app.services.read_state_service import ReadStateService
from tests.fakes import FakeBus, FakeCore, channel

pytestmark = pytest.mark.integration

ANA, BOB = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def make_room(sessions) -> AsyncGenerator[Callable[[], CoreChannel]]:
    """Fresh channels for Ana and Bob, with their rows deleted afterwards."""
    rooms: list[CoreChannel] = []

    def make() -> CoreChannel:
        room = channel(ANA, BOB)
        rooms.append(room)
        return room

    yield make
    async with sessions.begin() as session:
        for room in rooms:
            for model in (ChannelEvent, Message, ChannelCounter, ReadState):
                await session.execute(delete(model).where(model.channel_id == room.channel_id))


@pytest.fixture
def room(make_room) -> CoreChannel:
    return make_room()


def member(user_id: uuid.UUID, *rooms: CoreChannel) -> Memberships:
    """The user in the rooms, which are all in their own team, as chatty-core lists it."""
    return Memberships(
        user_id=user_id,
        is_active=True,
        teams=[
            TeamMembership(team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[])
            for room in rooms
        ],
        channels=[
            ChannelMembership.model_validate(
                {
                    "channel_id": room.channel_id,
                    "team_id": room.team_id,
                    "kind": "public",
                    "archived": False,
                    "role": "member",
                    "notify_level": "mentions",
                    "muted": False,
                    "hidden": False,
                }
            )
            for room in rooms
        ],
    )


def access(room: CoreChannel) -> AccessService:
    return AccessService(cast(CoreClient, FakeCore(channel=room)))


def messages(session: AsyncSession, room: CoreChannel) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        ReactionRepository(session),
        PinRepository(session),
        access(room),
        cast(EventBus, FakeBus()),
    )


def read_state(session: AsyncSession, room: CoreChannel, bus: EventBus) -> ReadStateService:
    return ReadStateService(
        session,
        ReadStateRepository(session),
        ChannelCounterRepository(session),
        access(room),
        bus,
    )


async def send(sessions, room: CoreChannel, author: uuid.UUID, body: str = "hi") -> MessageResponse:
    data = SendMessageRequest(client_msg_id=uuid.uuid4(), body=body)
    async with sessions() as session:
        message, _ = await messages(session, room).send(member(author, room), room.channel_id, data)
    return message


async def mark_read(
    sessions, room: CoreChannel, user_id: uuid.UUID, seq: int, bus: EventBus | None = None
) -> MarkReadResponse:
    async with sessions() as session:
        service = read_state(session, room, bus or cast(EventBus, FakeBus()))
        return await service.mark_read(member(user_id, room), room.channel_id, seq)


async def badge(sessions, room: CoreChannel, user_id: uuid.UUID) -> tuple[int, int]:
    """(unread, mentions) of the room, as GET /unread answers it."""
    async with sessions() as session:
        service = read_state(session, room, cast(EventBus, FakeBus()))
        response = await service.list_unread(member(user_id, room), room.team_id)
    [row] = response.channels
    assert isinstance(row, ChannelUnread)
    return row.unread, row.mentions


# New messages


async def test_a_new_message_is_unread_for_the_others(sessions, room):
    await send(sessions, room, ANA)
    await send(sessions, room, ANA)

    assert await badge(sessions, room, BOB) == (2, 0)


async def test_a_new_mention_counts_as_unread_and_as_a_mention(sessions, room):
    await send(sessions, room, ANA, f"hey <@{BOB}>")

    assert await badge(sessions, room, BOB) == (1, 1)


async def test_mentioning_someone_twice_is_one_mention(sessions, room):
    await send(sessions, room, ANA, f"<@{BOB}> <@{BOB}>")

    assert await badge(sessions, room, BOB) == (1, 1)


async def test_a_mention_of_someone_else_is_only_unread(sessions, room):
    await send(sessions, room, ANA, f"hey <@{uuid.uuid4()}>")

    assert await badge(sessions, room, BOB) == (1, 0)


async def test_a_message_after_my_read_position_is_unread_again(sessions, room):
    first = await send(sessions, room, ANA)
    await mark_read(sessions, room, BOB, first.seq)
    assert await badge(sessions, room, BOB) == (0, 0)

    await send(sessions, room, ANA)

    assert await badge(sessions, room, BOB) == (1, 0)


async def test_reading_part_of_a_channel_keeps_the_rest_unread(sessions, room):
    first = await send(sessions, room, ANA)
    await send(sessions, room, ANA, f"<@{BOB}>")

    result = await mark_read(sessions, room, BOB, first.seq)

    assert (result.unread, result.mentions) == (1, 1)
    assert await badge(sessions, room, BOB) == (1, 1)


async def test_each_user_has_their_own_counts(sessions, room):
    sent = await send(sessions, room, ANA)
    await send(sessions, room, BOB)

    await mark_read(sessions, room, BOB, sent.seq + 1)

    assert await badge(sessions, room, BOB) == (0, 0)
    assert await badge(sessions, room, ANA) == (1, 0)


async def test_reading_one_channel_leaves_the_others_unread(sessions, make_room):
    read, other = make_room(), make_room()
    sent = await send(sessions, read, ANA)
    await send(sessions, other, ANA)

    await mark_read(sessions, read, BOB, sent.seq)

    assert await badge(sessions, read, BOB) == (0, 0)
    assert await badge(sessions, other, BOB) == (1, 0)


# My own messages


async def test_my_own_message_is_not_unread_for_me(sessions, room):
    await send(sessions, room, ANA)

    assert await badge(sessions, room, ANA) == (0, 0)


async def test_mentioning_myself_is_not_unread(sessions, room):
    await send(sessions, room, ANA, f"<@{ANA}>")

    assert await badge(sessions, room, ANA) == (0, 0)


async def test_sending_marks_what_came_before_as_read(sessions, room):
    await send(sessions, room, BOB)
    await send(sessions, room, BOB, f"<@{ANA}>")
    assert await badge(sessions, room, ANA) == (2, 1)

    await send(sessions, room, ANA)

    assert await badge(sessions, room, ANA) == (0, 0)


async def test_a_message_after_my_own_is_unread(sessions, room):
    await send(sessions, room, ANA)
    await send(sessions, room, BOB)

    assert await badge(sessions, room, ANA) == (1, 0)


# Deleted messages


async def delete_message(sessions, room: CoreChannel, user_id: uuid.UUID, message_id) -> None:
    async with sessions() as session:
        await messages(session, room).delete(member(user_id, room), message_id)


async def test_deleting_an_unread_message_takes_it_off_the_count(sessions, room):
    await send(sessions, room, ANA)
    gone = await send(sessions, room, ANA)
    assert await badge(sessions, room, BOB) == (2, 0)

    await delete_message(sessions, room, ANA, gone.id)

    assert await badge(sessions, room, BOB) == (1, 0)


async def test_deleting_an_unread_mention_takes_it_off_both_counts(sessions, room):
    gone = await send(sessions, room, ANA, f"<@{BOB}>")
    assert await badge(sessions, room, BOB) == (1, 1)

    await delete_message(sessions, room, ANA, gone.id)

    assert await badge(sessions, room, BOB) == (0, 0)


async def test_deleting_a_read_message_changes_nothing(sessions, room):
    gone = await send(sessions, room, ANA)
    newest = await send(sessions, room, ANA)
    await mark_read(sessions, room, BOB, gone.seq)
    assert await badge(sessions, room, BOB) == (1, 0)

    await delete_message(sessions, room, ANA, gone.id)

    assert await badge(sessions, room, BOB) == (1, 0)
    assert (await mark_read(sessions, room, BOB, newest.seq)).unread == 0


async def test_deleting_takes_a_seq_but_doesnt_make_anything_unread(sessions, room):
    gone = await send(sessions, room, ANA)
    await mark_read(sessions, room, BOB, gone.seq)

    await delete_message(sessions, room, ANA, gone.id)

    assert await badge(sessions, room, BOB) == (0, 0)


async def test_a_message_after_a_delete_is_still_unread(sessions, room):
    gone = await send(sessions, room, ANA)
    await delete_message(sessions, room, ANA, gone.id)

    await send(sessions, room, ANA)

    assert await badge(sessions, room, BOB) == (1, 0)


# Edits and reactions take a seq, but they aren't messages


async def edit_message(sessions, room: CoreChannel, user_id: uuid.UUID, message_id, body) -> None:
    async with sessions() as session:
        await messages(session, room).edit(
            member(user_id, room), message_id, EditMessageRequest(body=body)
        )


async def test_editing_a_read_message_doesnt_make_it_unread(sessions, room):
    sent = await send(sessions, room, ANA)
    await mark_read(sessions, room, BOB, sent.seq)

    await edit_message(sessions, room, ANA, sent.id, "changed")

    assert await badge(sessions, room, BOB) == (0, 0)


async def test_a_mention_added_to_an_unread_message_counts(sessions, room):
    sent = await send(sessions, room, ANA)

    await edit_message(sessions, room, ANA, sent.id, f"<@{BOB}>")

    assert await badge(sessions, room, BOB) == (1, 1)


async def test_a_mention_removed_from_an_unread_message_stops_counting(sessions, room):
    sent = await send(sessions, room, ANA, f"<@{BOB}>")

    await edit_message(sessions, room, ANA, sent.id, "never mind")

    assert await badge(sessions, room, BOB) == (1, 0)


async def test_a_mention_added_to_a_read_message_isnt_unread(sessions, room):
    sent = await send(sessions, room, ANA)
    await mark_read(sessions, room, BOB, sent.seq)

    await edit_message(sessions, room, ANA, sent.id, f"<@{BOB}>")

    assert await badge(sessions, room, BOB) == (0, 0)


async def test_a_reaction_is_not_unread(sessions, room):
    sent = await send(sessions, room, ANA)
    await mark_read(sessions, room, BOB, sent.seq)

    async with sessions() as session:
        await ReactionService(
            session,
            MessageRepository(session),
            ReactionRepository(session),
            ChannelCounterRepository(session),
            ChannelEventRepository(session),
            access(room),
            cast(EventBus, FakeBus()),
        ).add(member(ANA, room), sent.id, "👍")

    assert await badge(sessions, room, BOB) == (0, 0)


async def test_reading_up_to_the_seq_of_an_edit_covers_the_messages_before_it(sessions, room):
    sent = await send(sessions, room, ANA)
    await edit_message(sessions, room, ANA, sent.id, "changed")  # takes seq 2

    result = await mark_read(sessions, room, BOB, 2)

    assert (result.last_read_seq, result.unread) == (2, 0)
