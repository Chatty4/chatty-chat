import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import (
    AppError,
    Conflict,
    InvalidFile,
    MessageAlreadySent,
    ServiceUnavailable,
    ValidationFailed,
)
from app.schemas.messages import ReactionResponse, SendMessageRequest
from tests.fakes import (
    FakeCore,
    FakeMessages,
    MessageFakes,
    attached_file,
    channel,
    memberships,
    saved_message,
    saved_pin,
)

USER_ID, OTHER_ID, OUTSIDER_ID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def request(body: str = "hi", **fields) -> SendMessageRequest:
    return SendMessageRequest(client_msg_id=uuid.uuid4(), body=body, **fields)


async def test_new_message_is_saved_then_published():
    room = channel(USER_ID, OTHER_ID)
    fakes = MessageFakes(FakeCore(channel=room))
    fakes.counters.last_seq = 7
    body = f"deploy? <@{OTHER_ID}> <@{OUTSIDER_ID}>"
    data = request(body)

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert created
    assert (message.seq, message.author_id, message.body) == (8, USER_ID, body)
    # in the response and, through payload below, in the event: the author's app matches on it
    assert message.client_msg_id == data.client_msg_id
    assert message.mentions == [OTHER_ID]  # the outsider stays in the text only
    assert (message.file_ids, message.attachments, message.reactions) == ([], [], [])
    payload = {"message": message.model_dump(mode="json")}
    assert fakes.events.rows == [(room.channel_id, 8, "message.created", payload)]
    assert fakes.read_state.positions == {(USER_ID, room.channel_id): 8}
    assert fakes.log == ["commit", "commit", "publish"]  # the retry lookup, the write, publish
    [(channel_id, event)] = fakes.bus.published
    assert channel_id == room.channel_id
    assert (event["type"], event["seq"], event["payload"]) == ("message.created", 8, payload)


async def test_retry_returns_the_original_without_a_new_event():
    room = channel(USER_ID)
    data = request()
    original = saved_message(room.channel_id, USER_ID, data.client_msg_id, seq=3)
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(existing=original))

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert not created
    assert (message.id, message.seq) == (original.id, 3)
    assert fakes.counters.last_seq == 0
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_retry_that_loses_the_race_returns_the_winner():
    room = channel(USER_ID)
    data = request()
    winner = saved_message(room.channel_id, USER_ID, data.client_msg_id)
    messages = FakeMessages(insert_error=MessageAlreadySent(), saved_meanwhile=winner)
    fakes = MessageFakes(FakeCore(channel=room), messages)

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert not created
    assert message.id == winner.id
    # the retry lookup, the write that lost, then the read of the winner with its reactions
    assert fakes.log == ["commit", "rollback", "commit"]
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_retry_returns_the_reactions_and_pin_the_message_has_now():
    room = channel(USER_ID)
    data = request()
    original = saved_message(room.channel_id, USER_ID, data.client_msg_id, seq=3)
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(existing=original))
    fakes.reactions.rows = [(original.id, OTHER_ID, "👍"), (original.id, USER_ID, "👍")]
    fakes.pins.rows = {original.id: saved_pin(original, OTHER_ID)}

    message, _ = await fakes.service.send(USER, room.channel_id, data)

    assert message.reactions == [ReactionResponse(emoji="👍", count=2, me=True)]
    assert message.pinned is True


async def test_retry_that_loses_the_race_returns_the_winners_reactions():
    room = channel(USER_ID)
    data = request()
    winner = saved_message(room.channel_id, USER_ID, data.client_msg_id)
    messages = FakeMessages(insert_error=MessageAlreadySent(), saved_meanwhile=winner)
    fakes = MessageFakes(FakeCore(channel=room), messages)
    fakes.reactions.rows = [(winner.id, OTHER_ID, "🎉")]

    message, _ = await fakes.service.send(USER, room.channel_id, data)

    assert message.reactions == [ReactionResponse(emoji="🎉", count=1, me=False)]


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_retry_after_the_channel_was_archived_returns_the_original(flags):
    room = channel(USER_ID, **flags)
    data = request()
    original = saved_message(room.channel_id, USER_ID, data.client_msg_id, seq=3)
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(existing=original))

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert not created
    assert message.id == original.id
    assert fakes.bus.published == []


async def test_client_msg_id_used_in_another_channel_is_400():
    room = channel(USER_ID)
    data = request()
    elsewhere = saved_message(uuid.uuid4(), USER_ID, data.client_msg_id)
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(existing=elsewhere))

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.send(USER, room.channel_id, data)

    assert error.value.fields == {"client_msg_id": "already_used"}
    assert fakes.counters.last_seq == 0
    assert fakes.bus.published == []


async def test_race_won_in_another_channel_is_400():
    room = channel(USER_ID)
    data = request()
    winner = saved_message(uuid.uuid4(), USER_ID, data.client_msg_id)
    messages = FakeMessages(insert_error=MessageAlreadySent(), saved_meanwhile=winner)
    fakes = MessageFakes(FakeCore(channel=room), messages)

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.send(USER, room.channel_id, data)

    assert error.value.fields == {"client_msg_id": "already_used"}
    assert fakes.bus.published == []


async def test_unique_violation_without_an_original_is_raised():
    room = channel(USER_ID)
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(insert_error=MessageAlreadySent()))

    with pytest.raises(MessageAlreadySent):
        await fakes.service.send(USER, room.channel_id, request())

    # the retry lookup, the write that failed, then the read that found no original
    assert fakes.log == ["commit", "rollback", "rollback"]
    assert fakes.bus.published == []


async def test_empty_body_without_files_is_refused_before_access():
    fakes = MessageFakes(FakeCore())  # no channel: access would be 404

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.send(USER, uuid.uuid4(), request(""))

    assert error.value.fields == {"body": "required"}
    assert fakes.log == []


@pytest.mark.parametrize(
    ("room", "status", "code"),
    [
        (None, 404, ErrorCode.CHANNEL_NOT_FOUND),
        (channel(OTHER_ID), 403, ErrorCode.NOT_A_MEMBER),
        (channel(USER_ID, archived=True), 409, ErrorCode.CHANNEL_ARCHIVED),
        (channel(USER_ID, team_deleted=True), 409, ErrorCode.CHANNEL_ARCHIVED),
    ],
)
async def test_user_who_cannot_write_changes_nothing(room, status, code):
    fakes = MessageFakes(FakeCore(channel=room))

    with pytest.raises(AppError) as error:
        await fakes.service.send(USER, uuid.uuid4(), request())

    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.log == ["commit"]  # only the retry lookup
    assert fakes.counters.last_seq == 0


# --- files (D-03) ---


def files_core(room, *files, **errors) -> FakeCore:
    return FakeCore(channel=room, attachments=list(files), **errors)


async def test_files_are_attached_before_the_write_and_returned_in_the_sent_order():
    room = channel(USER_ID)
    report, chart = attached_file("report.pdf"), attached_file("chart.png", "image/png")
    core = files_core(room, chart, report)  # chatty-core may answer in another order
    fakes = MessageFakes(core)
    data = request(file_ids=[report.file_id, chart.file_id])

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert created
    assert core.attach_calls == [(USER_ID, room.channel_id, [report.file_id, chart.file_id])]
    assert message.file_ids == [report.file_id, chart.file_id]
    assert [item.filename for item in message.attachments] == ["report.pdf", "chart.png"]
    stored = fakes.messages.attachments[message.id]
    assert [(row.file_id, row.position) for row in stored] == [
        (report.file_id, 0),
        (chart.file_id, 1),
    ]
    payload = {"message": message.model_dump(mode="json")}
    assert fakes.events.rows == [(room.channel_id, 1, "message.created", payload)]
    assert "object_key" not in str(payload)


async def test_a_message_without_files_doesnt_call_attach():
    room = channel(USER_ID)
    core = files_core(room)
    fakes = MessageFakes(core)

    await fakes.service.send(USER, room.channel_id, request())

    assert core.attach_calls == []


@pytest.mark.parametrize(
    "error",
    [
        InvalidFile("f6", "file_not_ready"),
        Conflict(ErrorCode.CHANNEL_ARCHIVED, "The channel's team was deleted"),
        ServiceUnavailable(),
    ],
    ids=["invalid_file", "channel_archived", "core_unavailable"],
)
async def test_attach_error_saves_nothing(error):
    room = channel(USER_ID)
    core = files_core(room, attach_error=error)
    fakes = MessageFakes(core)

    with pytest.raises(AppError) as raised:
        await fakes.service.send(USER, room.channel_id, request(file_ids=[uuid.uuid4()]))

    assert raised.value is error
    assert fakes.log == ["commit"]  # only the retry lookup: attach runs outside a transaction
    assert fakes.messages.inserted == []
    assert fakes.bus.published == []
    assert core.detach_calls == []  # nothing was attached, nothing to undo


async def test_the_same_file_twice_is_refused_before_chatty_core():
    room = channel(USER_ID)
    core = files_core(room)
    fakes = MessageFakes(core)
    file_id = uuid.uuid4()

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.send(USER, room.channel_id, request(file_ids=[file_id, file_id]))

    assert error.value.fields == {"file_ids": "invalid"}
    assert core.attach_calls == []
    assert fakes.log == []


async def test_retry_returns_the_original_with_its_files_without_attaching():
    room = channel(USER_ID)
    report = attached_file()
    data = request(file_ids=[report.file_id])
    original = saved_message(room.channel_id, USER_ID, data.client_msg_id, seq=3)
    core = files_core(room)
    fakes = MessageFakes(core, FakeMessages(existing=original))
    await fakes.messages.insert_attachments(original.id, [report])

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert not created
    assert message.file_ids == [report.file_id]
    assert core.attach_calls == []


class CoreSavedMeanwhile(FakeCore):
    """attach answers file_already_attached because a concurrent request with the same
    client_msg_id attached the files and saved `saved` first."""

    def __init__(self, room, messages: FakeMessages, saved) -> None:
        super().__init__(channel=room)
        self._messages = messages
        self._saved = saved

    async def attach(self, user_id, channel_id, file_ids):
        self._messages.existing = self._saved
        raise InvalidFile(str(file_ids[0]), "file_already_attached")


async def test_already_attached_by_a_concurrent_retry_returns_its_message():
    room = channel(USER_ID)
    data = request(file_ids=[uuid.uuid4()])
    saved = saved_message(room.channel_id, USER_ID, data.client_msg_id, seq=5)
    messages = FakeMessages()
    fakes = MessageFakes(CoreSavedMeanwhile(room, messages, saved), messages)

    message, created = await fakes.service.send(USER, room.channel_id, data)

    assert not created
    assert message.id == saved.id
    assert fakes.messages.inserted == []


async def test_already_attached_elsewhere_is_invalid_file():
    room = channel(USER_ID)
    error = InvalidFile("f6", "file_already_attached")
    fakes = MessageFakes(files_core(room, attach_error=error))

    with pytest.raises(InvalidFile):
        await fakes.service.send(USER, room.channel_id, request(file_ids=[uuid.uuid4()]))

    assert fakes.messages.inserted == []
