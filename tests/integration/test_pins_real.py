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
from app.models import ChannelCounter, ChannelEvent, Message, Pin, ReadState
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    PinRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel, Memberships
from app.schemas.messages import MessageResponse, SendMessageRequest
from app.schemas.pins import PinResponse
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from app.services.pin_service import PinService
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
    """A fresh channel id, and its rows deleted afterwards (pins go with their message)."""
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
        PinRepository(session),
        core(room),
        cast(EventBus, FakeBus()),
    )


def pin_service(session: AsyncSession, room: CoreChannel, bus: FakeBus) -> PinService:
    return PinService(
        session,
        MessageRepository(session),
        PinRepository(session),
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


async def pin(
    sessions, room: CoreChannel, bus: FakeBus, user: Memberships, message_id: uuid.UUID
) -> tuple[PinResponse, bool]:
    async with sessions() as session:
        return await pin_service(session, room, bus).pin(user, room.channel_id, message_id)


async def unpin(
    sessions, room: CoreChannel, bus: FakeBus, user: Memberships, message_id: uuid.UUID
) -> None:
    async with sessions() as session:
        await pin_service(session, room, bus).unpin(user, room.channel_id, message_id)


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


async def pinned_ids(sessions, channel_id: uuid.UUID) -> set[uuid.UUID]:
    async with sessions() as session:
        return set(
            await session.scalars(select(Pin.message_id).where(Pin.channel_id == channel_id))
        )


async def test_pin_twice_keeps_one_row_and_takes_one_seq(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()

    first, created = await pin(sessions, room, bus, USER, sent.id)
    again, created_again = await pin(sessions, room, bus, OTHER, sent.id)

    assert (created, created_again) == (True, False)
    assert again == first  # the existing pin, still by USER
    assert await pinned_ids(sessions, room.channel_id) == {sent.id}
    assert await last_seq(sessions, room.channel_id) == 2  # the second seq was rolled back
    assert await event_kinds(sessions, room.channel_id) == ["message.created", "pin.added"]
    assert len(bus.published) == 1


async def test_other_member_unpins_and_a_second_unpin_takes_no_seq(sessions, room):
    sent = await send(sessions, room)
    bus = FakeBus()
    await pin(sessions, room, bus, USER, sent.id)

    await unpin(sessions, room, bus, OTHER, sent.id)
    await unpin(sessions, room, bus, OTHER, sent.id)

    assert await pinned_ids(sessions, room.channel_id) == set()
    assert await last_seq(sessions, room.channel_id) == 3
    assert await event_kinds(sessions, room.channel_id) == [
        "message.created",
        "pin.added",
        "pin.removed",
    ]
    assert len(bus.published) == 2


async def test_history_and_the_pin_list_show_the_pin(sessions, room):
    pinned, other = await send(sessions, room), await send(sessions, room)
    await pin(sessions, room, FakeBus(), USER, pinned.id)

    async with sessions() as session:
        page = await message_service(session, room).list_history(USER, room.channel_id, None, 50)
        pins = await pin_service(session, room, FakeBus()).list_pins(USER, room.channel_id)

    assert {m.id: m.pinned for m in page.messages} == {pinned.id: True, other.id: False}
    assert [(p.message.id, p.message.pinned, p.pinned_by) for p in pins.pins] == [
        (pinned.id, True, USER_ID)
    ]


async def test_deleting_a_pinned_message_removes_the_pin_without_pin_removed(sessions, room):
    sent = await send(sessions, room)
    await pin(sessions, room, FakeBus(), USER, sent.id)

    await delete_message(sessions, room, sent.id)

    assert await pinned_ids(sessions, room.channel_id) == set()
    assert await event_kinds(sessions, room.channel_id) == [
        "message.created",
        "pin.added",
        "message.deleted",
    ]


async def test_two_pins_at_99_give_one_201_and_one_409(sessions, room):
    async with sessions.begin() as session:
        messages, pins = MessageRepository(session), PinRepository(session)
        for i in range(99):
            # seqs far above the counter, so they never meet the seqs it hands out
            message = await messages.insert_message(
                channel_id=room.channel_id,
                author_id=USER_ID,
                seq=1000 + i,
                client_msg_id=uuid.uuid4(),
                body="filler",
                mentions=[],
            )
            await pins.add_pin(room.channel_id, message.id, USER_ID)
    first, second = await send(sessions, room), await send(sessions, room)

    results = await asyncio.gather(
        pin(sessions, room, FakeBus(), USER, first.id),
        pin(sessions, room, FakeBus(), OTHER, second.id),
        return_exceptions=True,
    )

    created = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, AppError)]
    assert len(created) == 1 and len(refused) == 1
    assert refused[0].code == ErrorCode.PIN_LIMIT_REACHED
    assert len(await pinned_ids(sessions, room.channel_id)) == 100
    assert (await event_kinds(sessions, room.channel_id)).count("pin.added") == 1


async def test_pin_racing_a_delete_never_lands_after_it(sessions, room):
    sent = await send(sessions, room)

    results = await asyncio.gather(
        pin(sessions, room, FakeBus(), USER, sent.id),
        delete_message(sessions, room, sent.id),
        return_exceptions=True,
    )

    kinds = await event_kinds(sessions, room.channel_id)
    if isinstance(results[0], AppError):  # the delete went first
        assert results[0].code == ErrorCode.MESSAGE_DELETED
        assert kinds == ["message.created", "message.deleted"]
    else:  # the pin went first, and the delete removed it
        assert kinds == ["message.created", "pin.added", "message.deleted"]
    assert results[1] is None
    assert await pinned_ids(sessions, room.channel_id) == set()
