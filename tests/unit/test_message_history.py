import uuid
from datetime import UTC, datetime

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.models import Message
from app.schemas.core import CoreChannel, TeamMembership, TeamRole
from app.schemas.messages import ReactionResponse
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def messages_in(room: CoreChannel, *seqs: int) -> list[Message]:
    return [saved_message(room.channel_id, USER_ID, uuid.uuid4(), seq=seq) for seq in seqs]


def fakes_for(room: CoreChannel | None, stored: list[Message], last_seq: int = 0) -> MessageFakes:
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(stored=stored))
    fakes.counters.last_seq = last_seq
    return fakes


async def test_newest_page_is_highest_seq_first():
    room = channel(USER_ID)
    fakes = fakes_for(room, messages_in(room, 1, 2, 4, 5), last_seq=6)  # 3 and 6: edits

    page = await fakes.service.list_history(USER, room.channel_id, None, 3)

    assert [message.seq for message in page.messages] == [5, 4, 2]
    assert (page.has_more, page.last_seq) == (True, 6)


async def test_before_seq_returns_the_older_page():
    room = channel(USER_ID)
    fakes = fakes_for(room, messages_in(room, 1, 2, 4, 5), last_seq=6)

    page = await fakes.service.list_history(USER, room.channel_id, 2, 3)

    assert [message.seq for message in page.messages] == [1]
    assert page.has_more is False


async def test_channel_without_messages_is_empty():
    room = channel(USER_ID)

    page = await fakes_for(room, []).service.list_history(USER, room.channel_id, None, 50)

    assert (page.messages, page.has_more, page.last_seq) == ([], False, 0)


async def test_deleted_message_stays_in_its_place():
    room = channel(USER_ID)
    stored = messages_in(room, 1, 2)
    stored[0].body, stored[0].deleted_at = "", datetime.now(UTC)

    page = await fakes_for(room, stored).service.list_history(USER, room.channel_id, None, 50)

    assert [(m.seq, m.body, m.deleted_at is not None) for m in page.messages] == [
        (2, "hi", False),
        (1, "", True),
    ]


async def test_team_member_reads_a_public_channel_they_have_not_joined():
    room = channel(OTHER_ID)
    team = TeamMembership(
        team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[room.channel_id]
    )
    user = memberships(USER_ID, teams=[team])

    page = await fakes_for(room, messages_in(room, 1)).service.list_history(
        user, room.channel_id, None, 50
    )

    assert [message.seq for message in page.messages] == [1]


@pytest.mark.parametrize(
    ("core_has_channel", "status", "code"),
    [(True, 403, ErrorCode.CANNOT_READ_CHANNEL), (False, 404, ErrorCode.CHANNEL_NOT_FOUND)],
    ids=["private, not a member", "no channel"],
)
async def test_user_who_cannot_read_gets_an_error_before_any_query(core_has_channel, status, code):
    room = channel(OTHER_ID, kind="private")
    fakes = fakes_for(room if core_has_channel else None, messages_in(room, 1))

    with pytest.raises(AppError) as error:
        await fakes.service.list_history(USER, room.channel_id, None, 50)

    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.log == []  # no transaction was opened


async def test_history_returns_each_messages_reactions():
    room = channel(USER_ID, OTHER_ID)
    first, second = messages_in(room, 1, 2)
    fakes = fakes_for(room, [first, second])
    fakes.reactions.rows = [
        (first.id, OTHER_ID, "👍"),
        (first.id, USER_ID, "👍"),
        (first.id, OTHER_ID, "🎉"),
    ]

    page = await fakes.service.list_history(USER, room.channel_id, None, 50)

    by_seq = {message.seq: message.reactions for message in page.messages}
    assert by_seq[1] == [
        ReactionResponse(emoji="👍", count=2, me=True),
        ReactionResponse(emoji="🎉", count=1, me=False),
    ]
    assert by_seq[2] == []


async def test_me_is_for_the_user_who_asks():
    room = channel(USER_ID, OTHER_ID)
    [message] = messages_in(room, 1)
    fakes = fakes_for(room, [message])
    fakes.reactions.rows = [(message.id, OTHER_ID, "👍")]

    page = await fakes.service.list_history(USER, room.channel_id, None, 50)

    assert page.messages[0].reactions == [ReactionResponse(emoji="👍", count=1, me=False)]


async def test_reactions_are_loaded_once_for_the_page_only():
    room = channel(USER_ID)
    older, newer = messages_in(room, 1, 2)
    fakes = fakes_for(room, [older, newer])
    fakes.reactions.rows = [(older.id, USER_ID, "👍"), (newer.id, USER_ID, "🎉")]

    page = await fakes.service.list_history(USER, room.channel_id, None, 1)

    # one call, without the extra row that only tells has_more
    assert fakes.reactions.asked == [[newer.id]]
    assert page.messages[0].reactions == [ReactionResponse(emoji="🎉", count=1, me=True)]


async def test_a_deleted_message_shows_no_reactions():
    room = channel(USER_ID)
    [message] = messages_in(room, 1)
    message.deleted_at = datetime.now(UTC)
    fakes = fakes_for(room, [message])
    fakes.reactions.rows = [(message.id, USER_ID, "👍")]  # left from before the delete

    page = await fakes.service.list_history(USER, room.channel_id, None, 50)

    assert page.messages[0].reactions == []
