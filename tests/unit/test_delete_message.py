import uuid
from datetime import UTC, datetime

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError, ServiceUnavailable
from app.models import Message
from app.schemas.core import CoreChannel, Memberships, TeamMembership, TeamRole
from app.services import message_service
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

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def fakes_for(room: CoreChannel | None, message: Message, **messages) -> MessageFakes:
    return MessageFakes(FakeCore(channel=room), FakeMessages(stored=[message], **messages))


def message_in(room: CoreChannel, author_id: uuid.UUID = USER_ID) -> Message:
    message = saved_message(room.channel_id, author_id, uuid.uuid4(), seq=3)
    message.mentions = [OTHER_ID]
    return message


def team_member(room: CoreChannel, role: TeamRole, *, can_browse: bool = False) -> Memberships:
    """USER with `role` in the channel's team. `can_browse` lists the channel as public."""
    team = TeamMembership(
        team_id=room.team_id,
        role=role,
        public_channel_ids=[room.channel_id] if can_browse else [],
    )
    return memberships(USER_ID, teams=[team])


def assert_deleted(fakes: MessageFakes, message: Message, user: Memberships) -> None:
    """Soft-deleted with the next seq, and message.deleted published after the commit."""
    assert (message.body, message.mentions, message.seq) == ("", [], 3)
    assert message.deleted_at is not None
    [(channel_id, seq, kind, payload)] = fakes.events.rows
    assert (channel_id, kind) == (message.channel_id, "message.deleted")
    assert seq == fakes.counters.last_seq
    assert payload["message_id"] == str(message.id)
    assert payload["deleted_by"] == str(user.user_id)
    assert set(payload) == {"message_id", "deleted_by", "deleted_at"}
    assert fakes.log == ["commit", "commit", "publish"]  # the read, the write, then publish
    [(_, event)] = fakes.bus.published
    assert (event["type"], event["seq"], event["payload"]) == ("message.deleted", seq, payload)
    assert event["created_at"] == payload["deleted_at"]
    assert fakes.read_state.positions == {}  # deleting doesn't move the read position


async def assert_refused(
    fakes: MessageFakes, user: Memberships, message_id: uuid.UUID, status: int, code: str
) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.delete(user, message_id)
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.counters.last_seq == 0
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_author_deletes_their_message():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message)
    fakes.counters.last_seq = 7
    other_message_id = uuid.uuid4()
    fakes.reactions.rows = [
        (message.id, USER_ID, "👍"),
        (message.id, OTHER_ID, "🎉"),
        (other_message_id, USER_ID, "👍"),
    ]

    await fakes.service.delete(USER, message.id)

    assert fakes.counters.last_seq == 8
    assert_deleted(fakes, message, USER)
    # its own seq, not 8; never edited, so its message.edited events aren't scanned
    assert fakes.events.cleared == [(message.channel_id, 3, message.id, False)]
    # its reactions go too, without reaction.removed events; other messages keep theirs
    assert fakes.reactions.rows == [(other_message_id, USER_ID, "👍")]


async def test_deleting_an_edited_message_clears_its_edit_events():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    message.edited_at = datetime(2026, 1, 1, tzinfo=UTC)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert fakes.events.cleared == [(message.channel_id, 3, message.id, True)]


async def test_channel_owner_deletes_someone_elses_message():
    room = channel(OTHER_ID, owner=USER_ID)
    message = message_in(room, author_id=OTHER_ID)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert_deleted(fakes, message, USER)


@pytest.mark.parametrize("role", [TeamRole.OWNER, TeamRole.ADMIN])
async def test_team_owner_or_admin_deletes_someone_elses_message(role):
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    user = team_member(room, role)
    fakes = fakes_for(room, message)

    await fakes.service.delete(user, message.id)

    assert_deleted(fakes, message, user)


async def test_team_admin_who_is_not_in_the_public_channel_can_delete():
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    user = team_member(room, TeamRole.ADMIN, can_browse=True)
    fakes = fakes_for(room, message)

    await fakes.service.delete(user, message.id)

    assert_deleted(fakes, message, user)


async def test_other_member_is_403():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    user = team_member(room, TeamRole.MEMBER)

    await assert_refused(fakes_for(room, message), user, message.id, 403, ErrorCode.FORBIDDEN)


@pytest.mark.parametrize("who", ["channel owner", "team admin"])
async def test_only_the_author_can_delete_in_a_dm(who):
    if who == "channel owner":
        room = channel(OTHER_ID, owner=USER_ID, kind="dm")
        user = USER
    else:
        room = channel(USER_ID, OTHER_ID, kind="dm")
        user = team_member(room, TeamRole.ADMIN)
    message = message_in(room, author_id=OTHER_ID)

    await assert_refused(fakes_for(room, message), user, message.id, 403, ErrorCode.FORBIDDEN)


async def test_author_can_delete_in_a_dm():
    room = channel(USER_ID, OTHER_ID, kind="dm")
    message = message_in(room)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert_deleted(fakes, message, USER)


async def test_missing_message_is_404():
    room = channel(USER_ID)
    fakes = fakes_for(room, message_in(room))

    await assert_refused(fakes, USER, uuid.uuid4(), 404, ErrorCode.MESSAGE_NOT_FOUND)


async def test_message_in_channel_user_cannot_read_is_404():
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)

    await assert_refused(
        fakes_for(room, message), USER, message.id, 404, ErrorCode.MESSAGE_NOT_FOUND
    )


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409(flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)

    await assert_refused(
        fakes_for(room, message), USER, message.id, 409, ErrorCode.CHANNEL_ARCHIVED
    )


async def test_permission_is_checked_before_archived():
    room = channel(USER_ID, OTHER_ID, archived=True)
    message = message_in(room, author_id=OTHER_ID)

    await assert_refused(fakes_for(room, message), USER, message.id, 403, ErrorCode.FORBIDDEN)


async def test_already_deleted_message_is_204_without_an_event():
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)
    fakes = fakes_for(room, message)
    fakes.reactions.rows = [(message.id, USER_ID, "👍")]  # from before reactions were deleted

    await fakes.service.delete(USER, message.id)

    assert fakes.counters.last_seq == 0
    assert fakes.events.rows == []
    assert fakes.events.cleared == []  # cleared by the first delete
    assert fakes.reactions.rows == [(message.id, USER_ID, "👍")]  # this request deleted nothing
    assert fakes.log == ["commit"]  # only the read
    assert fakes.bus.published == []


async def test_message_deleted_during_the_delete_rolls_back_without_an_event():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, deleted_meanwhile=True)
    fakes.reactions.rows = [(message.id, USER_ID, "👍")]

    await fakes.service.delete(USER, message.id)  # no error: it is deleted either way

    assert fakes.log == ["commit", "rollback"]  # the seq taken in the write is given back
    assert fakes.reactions.rows == [(message.id, USER_ID, "👍")]  # left to the other delete
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_deleting_a_pinned_message_removes_its_pin_without_pin_removed():
    room = channel(USER_ID)
    message = message_in(room)
    other = saved_message(room.channel_id, USER_ID, uuid.uuid4(), seq=4)
    fakes = fakes_for(room, message)
    fakes.pins.rows = {
        pin.message_id: pin for pin in (saved_pin(message, USER_ID), saved_pin(other, USER_ID))
    }

    await fakes.service.delete(USER, message.id)

    assert_deleted(fakes, message, USER)  # message.deleted is the only event
    assert set(fakes.pins.rows) == {other.id}


# --- the message's files (D-03) ---


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(message_service, "DETACH_RETRY_SECONDS", 0)


async def with_files(room: CoreChannel, core: FakeCore, *names: str):
    """A message of USER with these files, and the fakes to delete it."""
    message = message_in(room)
    fakes = MessageFakes(core, FakeMessages(stored=[message]))
    files = [attached_file(name) for name in names]
    await fakes.messages.insert_attachments(message.id, files)
    return message, fakes, [file.file_id for file in files]


async def test_deleting_a_message_detaches_its_files_after_the_commit():
    room = channel(USER_ID)
    core = FakeCore(channel=room)
    message, fakes, file_ids = await with_files(room, core, "report.pdf", "chart.png")

    await fakes.service.delete(USER, message.id)

    assert core.detach_calls == [file_ids]
    assert fakes.log == ["commit", "commit", "publish"]  # detach comes after these
    assert fakes.messages.attachments[message.id]  # the rows stay


async def test_a_message_without_files_doesnt_detach():
    room = channel(USER_ID)
    core = FakeCore(channel=room)
    message = message_in(room)
    fakes = MessageFakes(core, FakeMessages(stored=[message]))

    await fakes.service.delete(USER, message.id)

    assert core.detach_calls == []


async def test_already_deleted_message_doesnt_detach_again():
    room = channel(USER_ID)
    core = FakeCore(channel=room)
    message, fakes, _ = await with_files(room, core, "report.pdf")
    message.deleted_at = datetime.now(UTC)

    await fakes.service.delete(USER, message.id)

    assert core.detach_calls == []


class CoreDetachFailsFirst(FakeCore):
    """detach fails `failures` times, then works."""

    def __init__(self, room: CoreChannel, failures: int) -> None:
        super().__init__(channel=room)
        self.failures = failures

    async def detach(self, file_ids):
        self.detach_calls.append(list(file_ids))
        if self.failures:
            self.failures -= 1
            raise ServiceUnavailable()


async def test_detach_is_retried(caplog):
    room = channel(USER_ID)
    core = CoreDetachFailsFirst(room, failures=2)
    message, fakes, file_ids = await with_files(room, core, "report.pdf")

    await fakes.service.delete(USER, message.id)

    assert core.detach_calls == [file_ids] * 3
    assert "Failed to detach" not in caplog.text


async def test_detach_that_keeps_failing_is_logged_and_the_delete_still_works(caplog):
    room = channel(USER_ID)
    core = FakeCore(channel=room, detach_error=ServiceUnavailable())
    message, fakes, file_ids = await with_files(room, core, "report.pdf")

    await fakes.service.delete(USER, message.id)

    assert core.detach_calls == [file_ids] * message_service.DETACH_ATTEMPTS
    assert f"Failed to detach files ['{file_ids[0]}']" in caplog.text
    assert message.deleted_at is not None
    assert fakes.bus.published  # message.deleted was still sent


async def test_deleting_a_replied_message_clears_its_quotes():
    room = channel(USER_ID)
    message = message_in(room)
    reply = saved_message(room.channel_id, OTHER_ID, uuid.uuid4(), seq=4)
    reply.reply_to_id = message.id
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(stored=[message, reply]))

    await fakes.service.delete(USER, message.id)

    assert fakes.events.quotes_cleared == [(room.channel_id, 3, message.id)]


async def test_deleting_a_message_without_replies_leaves_the_quotes_alone():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert fakes.events.quotes_cleared == []
