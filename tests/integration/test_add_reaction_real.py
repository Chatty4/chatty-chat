import asyncio
import uuid
from typing import cast

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.models import ChannelCounter, ChannelEvent, Message, Reaction, ReadState
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel, Memberships
from app.schemas.messages import MessageResponse, ReactionsResponse, SendMessageRequest
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from app.services.reaction_service import ReactionService
from tests.fakes import FakeBus, FakeCore, channel, memberships

pytestmark = pytest.mark.integration

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER, OTHER = memberships(USER_ID), memberships(OTHER_ID)


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def room(sessions):
    """A fresh channel id, and its rows deleted afterwards (reactions go with their message)."""
    room = channel(USER_ID, OTHER_ID)
    yield room
    async with sessions.begin() as session:
        for model in (ChannelEvent, Message, ChannelCounter, ReadState):
            await session.execute(delete(model).where(model.channel_id == room.channel_id))


def core(room: CoreChannel) -> AccessService:
    return AccessService(cast(CoreClient, FakeCore(channel=room)))


def message_service(session: AsyncSession, room: CoreChannel) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        ReactionRepository(session),
        core(room),
        cast(EventBus, FakeBus()),
    )


def reaction_service(session: AsyncSession, room: CoreChannel, bus: FakeBus) -> ReactionService:
    return ReactionService(
        session,
        MessageRepository(session),
        ReactionRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        core(room),
        cast(EventBus, bus),
    )


async def send(sessions, room: CoreChannel) -> MessageResponse:
    data = SendMessageRequest(client_msg_id=uuid.uuid4(), body="hi")
    async with sessions() as session:
        message, _ = await message_service(session, room).send(USER, room.channel_id, data)
    return message


async def delete_message(sessions, room: CoreChannel, message_id: uuid.UUID) -> None:
    async with sessions() as session:
        await message_service(session, room).delete(USER, message_id)


async def react(
    sessions,
    room: CoreChannel,
    bus: FakeBus,
    user: Memberships,
    message_id: uuid.UUID,
    emoji: str = "👍",
) -> ReactionsResponse:
    async with sessions() as session:
        return await reaction_service(session, room, bus).add(user, message_id, emoji)


async def unreact(
    sessions,
    room: CoreChannel,
    bus: FakeBus,
    user: Memberships,
    message_id: uuid.UUID,
    emoji: str = "👍",
) -> ReactionsResponse:
    async with sessions() as session:
        return await reaction_service(session, room, bus).remove(user, message_id, emoji)


async def history(
    sessions, room: CoreChannel, user: Memberships
) -> dict[uuid.UUID, list[tuple[str, int, bool]]]:
    """(emoji, count, me) per message of the newest page, as `user` sees it."""
    async with sessions() as session:
        page = await message_service(session, room).list_history(user, room.channel_id, None, 50)
    return {m.id: [(r.emoji, r.count, r.me) for r in m.reactions] for m in page.messages}


async def event_kinds(sessions, channel_id: uuid.UUID) -> list[str]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(ChannelEvent.type)
                .where(ChannelEvent.channel_id == channel_id)
                .order_by(ChannelEvent.seq)
            )
        )


async def last_seq(sessions, channel_id: uuid.UUID) -> int | None:
    async with sessions() as session:
        return await session.scalar(
            select(ChannelCounter.last_seq).where(ChannelCounter.channel_id == channel_id)
        )


async def reaction_rows(sessions, message_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(Reaction.user_id, Reaction.emoji).where(Reaction.message_id == message_id)
        )
        return [(user_id, emoji) for user_id, emoji in rows]


async def test_two_users_react_and_the_counts_add_up(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    await react(sessions, room, bus, USER, sent.id)
    await react(sessions, room, bus, USER, sent.id, "🎉")
    response = await react(sessions, room, bus, OTHER, sent.id)

    # in the order the emoji were first used; `me` is for the user who asked
    assert [(r.emoji, r.count, r.me) for r in response.reactions] == [
        ("👍", 2, True),
        ("🎉", 1, False),
    ]
    assert await event_kinds(sessions, room.channel_id) == [
        "message.created",
        "reaction.added",
        "reaction.added",
        "reaction.added",
    ]
    assert [event["seq"] for _, event in bus.published] == [2, 3, 4]
    assert bus.published[0][1]["payload"] == {
        "message_id": str(sent.id),
        "user_id": str(USER_ID),
        "emoji": "👍",
    }


async def test_same_emoji_twice_keeps_one_row_and_takes_no_seq(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    await react(sessions, room, bus, USER, sent.id)
    response = await react(sessions, room, bus, USER, sent.id)

    assert [(r.emoji, r.count) for r in response.reactions] == [("👍", 1)]
    assert await reaction_rows(sessions, sent.id) == [(USER_ID, "👍")]
    assert await last_seq(sessions, room.channel_id) == 2  # the second seq was rolled back
    assert len(bus.published) == 1


async def test_concurrent_same_reaction_writes_one_event(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    await asyncio.gather(
        react(sessions, room, bus, USER, sent.id),
        react(sessions, room, bus, USER, sent.id),
    )

    assert await reaction_rows(sessions, sent.id) == [(USER_ID, "👍")]
    assert await last_seq(sessions, room.channel_id) == 2
    assert len(bus.published) == 1


async def test_reacting_to_a_deleted_message_is_409_and_takes_no_seq(sessions, room):
    sent = await send(sessions, room)
    await delete_message(sessions, room, sent.id)

    with pytest.raises(AppError) as error:
        await react(sessions, room, FakeBus(), USER, sent.id)

    assert error.value.code == ErrorCode.MESSAGE_DELETED
    assert await last_seq(sessions, room.channel_id) == 2
    assert await reaction_rows(sessions, sent.id) == []


async def test_reaction_racing_a_delete_never_lands_after_it(sessions, room):
    sent = await send(sessions, room)

    results = await asyncio.gather(
        react(sessions, room, FakeBus(), USER, sent.id),
        delete_message(sessions, room, sent.id),
        return_exceptions=True,
    )

    kinds = await event_kinds(sessions, room.channel_id)
    if isinstance(results[0], AppError):  # the delete went first
        assert results[0].code == ErrorCode.MESSAGE_DELETED
        assert kinds == ["message.created", "message.deleted"]
    else:  # the reaction went first
        assert kinds == ["message.created", "reaction.added", "message.deleted"]
    assert results[1] is None


async def test_remove_deletes_my_row_and_sends_reaction_removed(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()
    await react(sessions, room, bus, USER, sent.id)
    await react(sessions, room, bus, OTHER, sent.id)

    response = await unreact(sessions, room, bus, USER, sent.id)

    assert [(r.emoji, r.count, r.me) for r in response.reactions] == [("👍", 1, False)]
    assert await reaction_rows(sessions, sent.id) == [(OTHER_ID, "👍")]
    assert await event_kinds(sessions, room.channel_id) == [
        "message.created",
        "reaction.added",
        "reaction.added",
        "reaction.removed",
    ]
    removed = bus.published[-1][1]
    assert (removed["type"], removed["seq"]) == ("reaction.removed", 4)
    assert removed["payload"] == {
        "message_id": str(sent.id),
        "user_id": str(USER_ID),
        "emoji": "👍",
    }


async def test_removing_a_reaction_i_dont_have_takes_no_seq(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    response = await unreact(sessions, room, bus, USER, sent.id)

    assert response.reactions == []
    assert await last_seq(sessions, room.channel_id) == 1  # rolled back
    assert bus.published == []


async def test_concurrent_removes_write_one_event(sessions, room):
    sent = await send(sessions, room)
    await react(sessions, room, FakeBus(), USER, sent.id)
    bus = FakeBus()

    await asyncio.gather(
        unreact(sessions, room, bus, USER, sent.id),
        unreact(sessions, room, bus, USER, sent.id),
    )

    assert await reaction_rows(sessions, sent.id) == []
    assert await last_seq(sessions, room.channel_id) == 3  # created, added, removed
    assert len(bus.published) == 1


async def test_history_shows_the_reactions_of_each_message(sessions, room):
    first = await send(sessions, room)
    second = await send(sessions, room)
    bus = FakeBus()
    await react(sessions, room, bus, USER, first.id)
    await react(sessions, room, bus, OTHER, first.id)
    await react(sessions, room, bus, OTHER, first.id, "🎉")

    mine = await history(sessions, room, USER)
    theirs = await history(sessions, room, OTHER)

    assert mine[first.id] == [("👍", 2, True), ("🎉", 1, False)]
    assert theirs[first.id] == [("👍", 2, True), ("🎉", 1, True)]
    assert mine[second.id] == theirs[second.id] == []


async def test_history_drops_a_removed_reaction(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()
    await react(sessions, room, bus, USER, sent.id)
    await unreact(sessions, room, bus, USER, sent.id)

    assert (await history(sessions, room, USER))[sent.id] == []
