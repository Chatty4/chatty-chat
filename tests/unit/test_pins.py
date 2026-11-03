import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.models import Message, Pin
from app.schemas.core import CoreChannel, Memberships
from app.schemas.messages import ReactionResponse
from tests.fakes import (
    FakeCore,
    FakeMessages,
    PinFakes,
    channel,
    memberships,
    saved_message,
    saved_pin,
)

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def message_in(room: CoreChannel, seq: int = 3) -> Message:
    return saved_message(room.channel_id, OTHER_ID, uuid.uuid4(), seq=seq)


def fakes_for(
    room: CoreChannel | None,
    stored: list[Message],
    pins: list[Pin] | None = None,
    **messages,
) -> PinFakes:
    return PinFakes(FakeCore(channel=room), FakeMessages(stored=stored, **messages), pins)


async def assert_pin_refused(
    fakes: PinFakes,
    channel_id: uuid.UUID,
    message_id: uuid.UUID,
    status: int,
    code: str,
    user: Memberships = USER,
) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.pin(user, channel_id, message_id)
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.events.rows == []
    assert fakes.bus.published == []


# pin


async def test_pin_takes_the_next_seq_and_sends_pin_added():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, [message])
    fakes.counters.last_seq = 7

    pin, created = await fakes.service.pin(USER, room.channel_id, message.id)

    assert created is True
    assert (pin.channel_id, pin.message_id, pin.pinned_by) == (
        room.channel_id,
        message.id,
        USER_ID,
    )
    [(channel_id, seq, kind, payload)] = fakes.events.rows
    assert (channel_id, seq, kind) == (room.channel_id, 8, "pin.added")
    assert payload == pin.model_dump(mode="json", exclude={"channel_id"})
    assert set(payload) == {"message_id", "pinned_by", "pinned_at"}
    assert fakes.log == ["commit", "commit", "publish"]  # read, write, then publish
    [(published_to, event)] = fakes.bus.published
    assert published_to == room.channel_id
    assert (event["type"], event["seq"], event["payload"]) == ("pin.added", 8, payload)


async def test_pinning_again_is_a_no_op_with_the_existing_pin():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    existing = saved_pin(message, OTHER_ID)
    fakes = fakes_for(room, [message], [existing])

    pin, created = await fakes.service.pin(USER, room.channel_id, message.id)

    assert created is False
    assert (pin.pinned_by, pin.pinned_at) == (OTHER_ID, existing.pinned_at)
    assert fakes.log == ["commit", "rollback"]  # the seq taken is given back
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_101st_pin_is_409_and_rolls_back():
    room = channel(USER_ID)
    pinned = [message_in(room, seq) for seq in range(1, 101)]
    message = message_in(room, 101)
    fakes = fakes_for(room, [*pinned, message], [saved_pin(m, USER_ID) for m in pinned])

    await assert_pin_refused(fakes, room.channel_id, message.id, 409, ErrorCode.PIN_LIMIT_REACHED)
    assert fakes.log == ["commit", "rollback"]
    assert message.id not in fakes.pins.rows


async def test_pins_of_other_channels_dont_count_towards_the_limit():
    room, elsewhere = channel(USER_ID), channel(USER_ID)
    others = [message_in(elsewhere, seq) for seq in range(1, 101)]
    message = message_in(room)
    fakes = fakes_for(room, [*others, message], [saved_pin(m, USER_ID) for m in others])

    _, created = await fakes.service.pin(USER, room.channel_id, message.id)

    assert created is True


async def test_pinning_a_deleted_message_is_409():
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)
    fakes = fakes_for(room, [message])

    await assert_pin_refused(fakes, room.channel_id, message.id, 409, ErrorCode.MESSAGE_DELETED)
    assert fakes.log == ["commit"]  # only the read


async def test_message_deleted_during_the_pin_is_409_and_rolls_back():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, [message], deleted_meanwhile=True)

    await assert_pin_refused(fakes, room.channel_id, message.id, 409, ErrorCode.MESSAGE_DELETED)
    assert fakes.log == ["commit", "rollback"]
    assert fakes.pins.rows == {}


async def test_message_in_another_channel_is_404():
    room, elsewhere = channel(USER_ID), channel(USER_ID)
    message = message_in(elsewhere)

    await assert_pin_refused(
        fakes_for(room, [message]), room.channel_id, message.id, 404, ErrorCode.MESSAGE_NOT_FOUND
    )


async def test_missing_message_is_404():
    room = channel(USER_ID)

    await assert_pin_refused(
        fakes_for(room, []), room.channel_id, uuid.uuid4(), 404, ErrorCode.MESSAGE_NOT_FOUND
    )


async def test_missing_channel_is_404():
    await assert_pin_refused(
        fakes_for(None, []), uuid.uuid4(), uuid.uuid4(), 404, ErrorCode.CHANNEL_NOT_FOUND
    )


async def test_non_member_is_403():
    room = channel(OTHER_ID)
    message = message_in(room)

    await assert_pin_refused(
        fakes_for(room, [message]), room.channel_id, message.id, 403, ErrorCode.NOT_A_MEMBER
    )


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409(flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)

    await assert_pin_refused(
        fakes_for(room, [message]), room.channel_id, message.id, 409, ErrorCode.CHANNEL_ARCHIVED
    )


# unpin


async def test_member_who_didnt_pin_can_unpin_and_it_sends_pin_removed():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    fakes = fakes_for(room, [message], [saved_pin(message, OTHER_ID)])
    fakes.counters.last_seq = 7

    await fakes.service.unpin(USER, room.channel_id, message.id)

    assert fakes.pins.rows == {}
    [(channel_id, seq, kind, payload)] = fakes.events.rows
    assert (channel_id, seq, kind) == (room.channel_id, 8, "pin.removed")
    assert payload == {"message_id": str(message.id), "unpinned_by": str(USER_ID)}
    assert fakes.log == ["commit", "commit", "publish"]
    [(_, event)] = fakes.bus.published
    assert (event["type"], event["seq"], event["payload"]) == ("pin.removed", 8, payload)


async def test_unpinning_a_message_that_isnt_pinned_is_a_no_op():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, [message])

    await fakes.service.unpin(USER, room.channel_id, message.id)

    assert fakes.log == ["commit", "rollback"]  # the seq taken is given back
    assert fakes.events.rows == []
    assert fakes.bus.published == []


@pytest.mark.parametrize(
    ("members", "flags", "status", "code"),
    [
        ((OTHER_ID,), {}, 403, ErrorCode.NOT_A_MEMBER),
        ((USER_ID,), {"archived": True}, 409, ErrorCode.CHANNEL_ARCHIVED),
    ],
    ids=["not a member", "archived"],
)
async def test_unpin_is_refused(members, flags, status, code):
    room = channel(*members, **flags)
    message = message_in(room)
    fakes = fakes_for(room, [message], [saved_pin(message, OTHER_ID)])

    with pytest.raises(AppError) as error:
        await fakes.service.unpin(USER, room.channel_id, message.id)

    assert (error.value.status_code, error.value.code) == (status, code)
    assert message.id in fakes.pins.rows
    assert fakes.events.rows == []


async def test_unpin_of_a_message_in_another_channel_is_404():
    room, elsewhere = channel(USER_ID), channel(USER_ID)
    message = message_in(elsewhere)
    fakes = fakes_for(room, [message], [saved_pin(message, USER_ID)])

    with pytest.raises(AppError) as error:
        await fakes.service.unpin(USER, room.channel_id, message.id)

    assert error.value.code == ErrorCode.MESSAGE_NOT_FOUND
    assert message.id in fakes.pins.rows


# list


async def test_list_is_newest_pin_first_with_full_messages():
    room = channel(USER_ID, OTHER_ID)
    older, newer = message_in(room, 1), message_in(room, 2)
    now = datetime.now(UTC)
    fakes = fakes_for(
        room,
        [older, newer],
        [saved_pin(newer, USER_ID, now - timedelta(hours=1)), saved_pin(older, OTHER_ID, now)],
    )
    fakes.reactions.rows = [(newer.id, OTHER_ID, "👍")]

    response = await fakes.service.list_pins(USER, room.channel_id)

    assert [(p.message.id, p.pinned_by) for p in response.pins] == [
        (older.id, OTHER_ID),
        (newer.id, USER_ID),
    ]
    assert all(p.message.pinned for p in response.pins)
    assert response.pins[1].message.reactions == [ReactionResponse(emoji="👍", count=1, me=False)]
    assert fakes.reactions.asked == [[older.id, newer.id]]  # one query for every pin


async def test_list_without_pins_is_empty():
    room = channel(USER_ID)

    response = await fakes_for(room, []).service.list_pins(USER, room.channel_id)

    assert response.pins == []


@pytest.mark.parametrize(
    ("room", "status", "code"),
    [
        (None, 404, ErrorCode.CHANNEL_NOT_FOUND),
        (channel(OTHER_ID, kind="private"), 403, ErrorCode.CANNOT_READ_CHANNEL),
    ],
    ids=["missing", "private"],
)
async def test_list_is_refused(room, status, code):
    with pytest.raises(AppError) as error:
        await fakes_for(room, []).service.list_pins(USER, uuid.uuid4())

    assert (error.value.status_code, error.value.code) == (status, code)
