"""Forwarding with POST /channels/{channel_id}/messages and forward_of (D-20)."""

import uuid
from datetime import UTC, datetime

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import BadRequest, InvalidFile, NotFound, ValidationFailed
from app.models import Message
from app.schemas.core import CoreChannel, TeamMembership, TeamRole
from app.schemas.messages import SendMessageRequest
from tests.fakes import (
    FakeCore,
    FakeMessages,
    MessageFakes,
    attached_file,
    channel,
    memberships,
    saved_message,
)

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


class DatabaseDown(Exception):
    pass


def forward(source_id: uuid.UUID, **fields) -> SendMessageRequest:
    return SendMessageRequest(client_msg_id=uuid.uuid4(), forward_of=source_id, **fields)


def in_team_of(room: CoreChannel, source_room: CoreChannel) -> CoreChannel:
    return source_room.model_copy(update={"team_id": room.team_id})


def source_in(source_room: CoreChannel) -> Message:
    return saved_message(source_room.channel_id, OTHER_ID, uuid.uuid4(), seq=4)


async def test_a_forward_copies_the_body_but_not_the_mentions_or_the_reply():
    room = channel(USER_ID, OTHER_ID)
    source_room = in_team_of(room, channel(USER_ID, OTHER_ID))
    source = source_in(source_room)
    source.body, source.mentions, source.reply_to_id = f"ok? <@{USER_ID}>", [USER_ID], uuid.uuid4()
    core = FakeCore(channel=room, channels=[source_room])
    fakes = MessageFakes(core, FakeMessages(stored=[source]))

    message, created = await fakes.service.send(USER, room.channel_id, forward(source.id))

    assert created
    [saved] = fakes.messages.inserted
    assert (saved.body, saved.mentions, saved.reply_to_id) == (source.body, [], None)
    assert saved.forwarded_from_id == source.id
    assert (message.author_id, message.forwarded, message.reply_to) == (USER_ID, True, None)
    [(_, _, _, payload)] = fakes.events.rows
    assert payload["message"]["forwarded"] is True
    assert str(source.id) not in str(payload)  # never says from where
    assert (core.attach_calls, core.copy_calls) == ([], [])


async def test_a_forward_copies_the_files_and_their_labels():
    room = channel(USER_ID)
    source_room = in_team_of(room, channel(USER_ID))
    source = source_in(source_room)
    report, chart = attached_file(), attached_file("chart.png", "image/png")
    messages = FakeMessages(stored=[source])
    await messages.insert_attachments(source.id, [report, chart])
    copies = [attached_file(), attached_file("chart.png", "image/png")]
    core = FakeCore(channel=room, channels=[source_room], copies=copies)
    fakes = MessageFakes(core, messages)

    message, _ = await fakes.service.send(USER, room.channel_id, forward(source.id))

    assert core.copy_calls == [(USER_ID, room.channel_id, [report.file_id, chart.file_id])]
    assert core.attach_calls == []
    assert message.file_ids == [copy.file_id for copy in copies]
    assert messages.copied_labels == {
        report.file_id: copies[0].file_id,
        chart.file_id: copies[1].file_id,
    }


@pytest.mark.parametrize("case", ["missing", "deleted", "not_readable"])
async def test_a_source_the_user_cant_see_is_404(case):
    room = channel(USER_ID)
    members = (OTHER_ID,) if case == "not_readable" else (USER_ID,)
    source_room = in_team_of(room, channel(*members, kind="private"))
    source = source_in(source_room)
    if case == "deleted":
        source.deleted_at = datetime.now(UTC)
    core = FakeCore(channel=room, channels=[source_room], copies=[attached_file()])
    fakes = MessageFakes(core, FakeMessages(stored=[] if case == "missing" else [source]))

    with pytest.raises(NotFound) as error:
        await fakes.service.send(USER, room.channel_id, forward(source.id))

    assert error.value.code == ErrorCode.MESSAGE_NOT_FOUND
    assert fakes.messages.inserted == []
    assert core.copy_calls == []


async def test_a_public_source_the_user_only_views_can_be_forwarded():
    room = channel(USER_ID)
    source_room = in_team_of(room, channel(OTHER_ID, kind="public"))
    source = source_in(source_room)
    viewer = memberships(
        USER_ID,
        teams=[TeamMembership(team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[])],
    )
    core = FakeCore(channel=room, channels=[source_room])
    fakes = MessageFakes(core, FakeMessages(stored=[source]))

    message, created = await fakes.service.send(viewer, room.channel_id, forward(source.id))

    assert created
    assert message.forwarded


async def test_a_source_of_another_team_is_400():
    room = channel(USER_ID)
    source_room = channel(USER_ID)  # another team_id
    source = source_in(source_room)
    core = FakeCore(channel=room, channels=[source_room], copies=[attached_file()])
    fakes = MessageFakes(core, FakeMessages(stored=[source]))

    with pytest.raises(BadRequest) as error:
        await fakes.service.send(USER, room.channel_id, forward(source.id))

    assert error.value.code == ErrorCode.FORWARD_WRONG_TEAM
    assert fakes.messages.inserted == []
    assert core.copy_calls == []


@pytest.mark.parametrize(
    "extra",
    [{"body": "hi"}, {"file_ids": [uuid.uuid4()]}, {"reply_to_id": uuid.uuid4()}],
    ids=["body", "file_ids", "reply_to_id"],
)
async def test_forward_of_with_other_fields_is_refused_first(extra):
    fakes = MessageFakes(FakeCore())

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.send(USER, uuid.uuid4(), forward(uuid.uuid4(), **extra))

    assert error.value.fields == {"forward_of": "invalid"}
    assert fakes.log == []


async def test_a_source_deleted_during_the_copy_is_404():
    room = channel(USER_ID)
    source_room = in_team_of(room, channel(USER_ID))
    source = source_in(source_room)
    messages = FakeMessages(stored=[source])
    await messages.insert_attachments(source.id, [attached_file()])
    error = InvalidFile("f6", "file_not_found")
    core = FakeCore(channel=room, channels=[source_room], copy_error=error)
    fakes = MessageFakes(core, messages)

    with pytest.raises(NotFound) as raised:
        await fakes.service.send(USER, room.channel_id, forward(source.id))

    assert raised.value.code == ErrorCode.MESSAGE_NOT_FOUND
    assert fakes.messages.inserted == []
    assert core.detach_calls == []  # nothing was copied


async def test_a_failed_save_detaches_the_copies():
    room = channel(USER_ID)
    source_room = in_team_of(room, channel(USER_ID))
    source = source_in(source_room)
    messages = FakeMessages(stored=[source], insert_error=DatabaseDown())
    await messages.insert_attachments(source.id, [attached_file()])
    copy = attached_file()
    core = FakeCore(channel=room, channels=[source_room], copies=[copy])
    fakes = MessageFakes(core, messages)

    with pytest.raises(DatabaseDown):
        await fakes.service.send(USER, room.channel_id, forward(source.id))

    assert core.detach_calls == [[copy.file_id]]
    assert fakes.bus.published == []


async def test_a_retry_returns_the_forward_without_copying_again():
    room = channel(USER_ID)
    source_room = in_team_of(room, channel(USER_ID))
    source = source_in(source_room)
    data = forward(source.id)
    original = saved_message(room.channel_id, USER_ID, data.client_msg_id, seq=5)
    original.forwarded_from_id = source.id
    core = FakeCore(channel=room, channels=[source_room], copies=[attached_file()])
    fakes = MessageFakes(core, FakeMessages(existing=original, stored=[source]))

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert not created
    assert (message.id, message.forwarded) == (original.id, True)
    assert core.copy_calls == []
