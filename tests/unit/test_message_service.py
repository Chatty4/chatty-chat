import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError, MessageAlreadySent, ValidationFailed
from app.schemas.messages import SendMessageRequest
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID, OUTSIDER_ID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def request(body: str = "hi", **fields) -> SendMessageRequest:
    return SendMessageRequest(client_msg_id=uuid.uuid4(), body=body, **fields)


async def test_new_message_is_saved_then_published():
    room = channel(USER_ID, OTHER_ID)
    fakes = MessageFakes(FakeCore(channel=room))
    fakes.counters.last_seq = 7
    body = f"deploy? <@{OTHER_ID}> <@{OUTSIDER_ID}>"

    message, created = await fakes.service.send(USER, room.channel_id, request(body))

    assert created
    assert (message.seq, message.author_id, message.body) == (8, USER_ID, body)
    assert message.mentions == [OTHER_ID]  # the outsider stays in the text only
    assert (message.file_ids, message.attachments, message.reactions) == ([], [], [])
    payload = {"message": message.model_dump(mode="json")}
    assert fakes.events.rows == [(room.channel_id, 8, "message.created", payload)]
    assert fakes.read_state.positions == {(USER_ID, room.channel_id): 8}
    assert fakes.log == ["commit", "publish"]
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
    assert fakes.log == ["rollback"]
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_unique_violation_without_an_original_is_raised():
    room = channel(USER_ID)
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(insert_error=MessageAlreadySent()))

    with pytest.raises(MessageAlreadySent):
        await fakes.service.send(USER, room.channel_id, request())

    assert fakes.log == ["rollback"]
    assert fakes.bus.published == []


async def test_empty_body_without_files_is_refused_before_access():
    fakes = MessageFakes(FakeCore())  # no channel: access would be 404

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.send(USER, uuid.uuid4(), request(""))

    assert error.value.fields == {"body": "required"}
    assert fakes.log == []


async def test_files_are_refused_until_attachments_exist():
    room = channel(USER_ID)
    fakes = MessageFakes(FakeCore(channel=room))

    with pytest.raises(AppError) as error:
        await fakes.service.send(USER, room.channel_id, request(file_ids=[uuid.uuid4()]))

    assert (error.value.status_code, error.value.code) == (400, ErrorCode.INVALID_FILE)
    assert fakes.counters.last_seq == 0
    assert fakes.log == ["rollback"]
    assert fakes.bus.published == []


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
    assert fakes.log == []
    assert fakes.counters.last_seq == 0
