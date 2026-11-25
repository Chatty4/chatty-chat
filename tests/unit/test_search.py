import base64
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.models import Attachment
from app.repositories import MessageRepository, PinRepository, ReactionRepository
from app.repositories.messages import HIGHLIGHT_START, HIGHLIGHT_STOP
from app.schemas.core import (
    ChannelKind,
    ChannelMembership,
    ChannelRole,
    Memberships,
    NotifyLevel,
    TeamMembership,
    TeamRole,
)
from app.services.access_service import AccessService
from app.services.search_service import (
    SearchService,
    decode_cursor,
    encode_cursor,
    readable_channel_ids,
    to_highlight,
)
from tests.fakes import FakeCore, FakePins, FakeReactions, saved_message

USER_ID = uuid.uuid4()
TEAM, OTHER_TEAM = uuid.uuid4(), uuid.uuid4()
JOINED, PUBLIC, ELSEWHERE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def member_of(channel_id: uuid.UUID, team_id: uuid.UUID) -> ChannelMembership:
    return ChannelMembership(
        channel_id=channel_id,
        team_id=team_id,
        kind=ChannelKind.PRIVATE,
        archived=False,
        role=ChannelRole.MEMBER,
        notify_level=NotifyLevel.ALL,
        muted=False,
        hidden=False,
    )


USER = Memberships(
    user_id=USER_ID,
    is_active=True,
    teams=[
        TeamMembership(team_id=TEAM, role=TeamRole.MEMBER, public_channel_ids=[PUBLIC, JOINED]),
        TeamMembership(team_id=OTHER_TEAM, role=TeamRole.MEMBER, public_channel_ids=[]),
    ],
    channels=[member_of(JOINED, TEAM), member_of(ELSEWHERE, OTHER_TEAM)],
)


class FakeSearchMessages:
    """Returns `count` matching rows and records what the service asked for."""

    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.calls: list[dict[str, Any]] = []

    async def search_messages(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.append(kwargs)
        return [
            SimpleNamespace(
                Message=saved_message(JOINED, USER_ID, uuid.uuid4(), seq=seq),
                rank=0.5,
                highlight=f"{HIGHLIGHT_START}hi{HIGHLIGHT_STOP}",
            )
            for seq in range(min(self.count, kwargs["limit"]), 0, -1)
        ]

    async def list_attachments(self, message_ids: list[uuid.UUID]) -> dict:
        return {}

    async def search_attachments(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.append(kwargs)
        message = saved_message(JOINED, USER_ID, uuid.uuid4(), seq=1)
        return [
            SimpleNamespace(
                Attachment=Attachment(
                    file_id=uuid.uuid4(), filename="cat.jpg", mime="image/jpeg", size=10
                ),
                Message=message,  # both files are on one message
                rank=0.5,
                highlight=f"{HIGHLIGHT_START}orange{HIGHLIGHT_STOP}, cat",
            )
            for _ in range(min(self.count, kwargs["limit"]))
        ]

    async def get_messages(self, message_ids: list[uuid.UUID]) -> dict:
        return {}


def search_service(messages: FakeSearchMessages) -> SearchService:
    return SearchService(
        cast(MessageRepository, messages),
        cast(ReactionRepository, FakeReactions()),
        cast(PinRepository, FakePins()),
        AccessService(cast(CoreClient, FakeCore())),
    )


async def search(
    service: SearchService, *, channel_id: uuid.UUID | None = None, cursor: str | None = None
):
    return await service.search(USER, TEAM, "hi", channel_id, None, None, None, cursor, 2)


def test_highlight_escapes_the_users_html_and_keeps_our_mark():
    raw = f"<script>x</script> {HIGHLIGHT_START}deploy{HIGHLIGHT_STOP} & 1 < 2"

    assert to_highlight(raw) == (
        "&lt;script&gt;x&lt;/script&gt; <mark>deploy</mark> &amp; 1 &lt; 2"
    )


def test_cursor_round_trips():
    key = (0.0607927106320858, datetime(2026, 10, 5, 14, 2, 11, 123456, UTC), uuid.uuid4())

    assert decode_cursor(encode_cursor(key, "filters"), "filters") == key


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b'{"f": "filters"}').decode(),
        base64.urlsafe_b64encode(b'{"k": [1, "not a date", "x"], "f": "filters"}').decode(),
    ],
)
def test_broken_cursor_is_invalid_cursor(cursor):
    with pytest.raises(AppError) as exc:
        decode_cursor(cursor, "filters")

    assert (exc.value.status_code, exc.value.code) == (400, ErrorCode.INVALID_CURSOR)


def test_cursor_from_another_search_is_invalid_cursor():
    cursor = encode_cursor((0.5, datetime.now(UTC), uuid.uuid4()), "deploy filters")

    with pytest.raises(AppError) as exc:
        decode_cursor(cursor, "release filters")

    assert exc.value.code == ErrorCode.INVALID_CURSOR


def test_readable_channels_are_joined_and_public_ones_of_that_team_only():
    assert sorted(readable_channel_ids(USER, TEAM)) == sorted([JOINED, PUBLIC])


async def test_a_team_the_user_is_not_in_is_403():
    service = search_service(FakeSearchMessages())

    with pytest.raises(AppError) as exc:
        await service.search(USER, uuid.uuid4(), "hi", None, None, None, None, None, 20)

    assert (exc.value.status_code, exc.value.code) == (403, ErrorCode.NOT_A_TEAM_MEMBER)


async def test_in_a_readable_channel_searches_only_it():
    messages = FakeSearchMessages()

    await search(search_service(messages), channel_id=PUBLIC)

    assert messages.calls[0]["channel_ids"] == [PUBLIC]


async def test_in_a_channel_the_user_cant_read_finds_nothing():
    messages = FakeSearchMessages()

    page = await search(search_service(messages), channel_id=ELSEWHERE)

    assert messages.calls[0]["channel_ids"] == []
    assert page.results == []


async def test_a_full_page_has_a_next_cursor_that_continues_after_its_last_result():
    messages = FakeSearchMessages(count=5)
    service = search_service(messages)

    first = await search(service)
    await search(service, cursor=first.next_cursor)

    assert len(first.results) == 2
    assert messages.calls[0]["limit"] == 3  # one extra row tells a next page exists
    last = first.results[-1].message
    assert messages.calls[1]["cursor"] == (0.5, last.created_at, last.id)


async def test_the_last_page_has_no_next_cursor():
    page = await search(search_service(FakeSearchMessages(count=1)))

    assert len(page.results) == 1
    assert page.next_cursor is None
    assert page.results[0].highlight == "<mark>hi</mark>"


def test_highlight_never_marks_inside_a_mention():
    mention = f"546256e4-2e02-47{HIGHLIGHT_START}cf{HIGHLIGHT_STOP}-a7c1-78f3401e3b4b"
    raw = f"<@{mention}> {HIGHLIGHT_START}cf{HIGHLIGHT_STOP}"

    assert to_highlight(raw) == ("&lt;@546256e4-2e02-47cf-a7c1-78f3401e3b4b&gt; <mark>cf</mark>")


async def search_files(service: SearchService, cursor: str | None = None):
    return await service.search_attachments(USER, TEAM, "orange", None, None, None, None, cursor, 2)


async def test_file_results_have_the_file_its_message_and_a_safe_highlight():
    page = await search_files(search_service(FakeSearchMessages(count=1)))

    [result] = page.results
    assert result.attachment.filename == "cat.jpg"
    assert result.message.channel_id == JOINED
    assert result.highlight == "<mark>orange</mark>, cat"
    assert page.next_cursor is None


async def test_file_cursor_continues_after_the_last_file():
    messages = FakeSearchMessages(count=5)
    service = search_service(messages)

    first = await search_files(service)
    await search_files(service, cursor=first.next_cursor)

    last = first.results[-1]
    assert messages.calls[1]["cursor"] == (0.5, last.message.created_at, last.attachment.file_id)


async def test_a_message_search_cursor_is_invalid_for_files():
    messages = FakeSearchMessages(count=5)
    service = search_service(messages)
    message_page = await service.search(USER, TEAM, "orange", None, None, None, None, None, 2)

    with pytest.raises(AppError) as error:
        await search_files(service, cursor=message_page.next_cursor)

    assert error.value.code == ErrorCode.INVALID_CURSOR
