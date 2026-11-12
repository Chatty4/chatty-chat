import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.core import CoreClient
from app.clients.presence import PresenceStore
from app.clients.web_push import PushDedupe, WebPushClient
from app.core.exceptions import ServiceUnavailable
from app.models import PushSubscription
from app.repositories import PushSubscriptionRepository
from app.schemas.core import CoreChannel
from app.schemas.events import ChannelEvent
from app.services.push_service import PREVIEW_CHARS, PushService, push_recipients
from tests.fakes import (
    FakeCore,
    FakeDedupe,
    FakePresence,
    FakeSender,
    FakeSession,
    FakeSubscriptions,
)

AUTHOR, ANA, BOB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def member_channel(
    *members: tuple[uuid.UUID, str, bool], archived: bool = False, team_deleted: bool = False
) -> CoreChannel:
    """The author (notify_level all) plus `members` as (user_id, notify_level, muted)."""
    return CoreChannel.model_validate(
        {
            "channel_id": uuid.uuid4(),
            "team_id": uuid.uuid4(),
            "kind": "public",
            "archived": archived,
            "team_deleted": team_deleted,
            "members": [
                {"user_id": user_id, "role": "member", "notify_level": level, "muted": muted}
                for user_id, level, muted in [(AUTHOR, "all", False), *members]
            ],
        }
    )


def subscription(user_id: uuid.UUID) -> PushSubscription:
    return PushSubscription(
        id=uuid.uuid4(),
        user_id=user_id,
        endpoint=f"https://push.example.test/{uuid.uuid4()}",
        p256dh="key",
        auth="secret",
    )


def created(
    channel: CoreChannel,
    *,
    mentions: tuple[uuid.UUID, ...] = (),
    body: str | None = "hello",
    age: timedelta = timedelta(0),
    event_type: str = "message.created",
    message: dict[str, Any] | None = None,
) -> ChannelEvent:
    if message is None:
        message = {
            "id": str(uuid.uuid4()),
            "author_id": str(AUTHOR),
            "body": body,
            "mentions": [str(user_id) for user_id in mentions],
        }
    return ChannelEvent(
        type=event_type,
        channel_id=channel.channel_id,
        seq=7,
        created_at=datetime.now(UTC) - age,
        payload={"message": message},
    )


class Fakes:
    def __init__(
        self,
        channel: CoreChannel | None,
        *rows: PushSubscription,
        gone: tuple[str, ...] = (),
        core_error: Exception | None = None,
    ) -> None:
        self.presence = FakePresence()
        self.subscriptions = FakeSubscriptions(*rows)
        self.sender = FakeSender(gone)
        self.log: list[str] = []
        self.service = PushService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(PushSubscriptionRepository, self.subscriptions),
            cast(CoreClient, FakeCore(channel=channel, error=core_error)),
            cast(PresenceStore, self.presence),
            cast(PushDedupe, FakeDedupe()),
            cast(WebPushClient, self.sender),
        )

    @property
    def pushed(self) -> list[uuid.UUID]:
        return [user_id for user_id, _ in self.sender.sent]


# push_recipients


@pytest.mark.parametrize(
    ("level", "muted", "mentioned", "expected"),
    [
        ("all", False, False, True),
        ("mentions", False, True, True),
        ("mentions", False, False, False),
        ("none", False, True, False),
        ("all", True, False, False),
        ("mentions", True, True, False),  # muted wins over a mention
    ],
)
def test_push_recipients_follow_the_member_settings(level, muted, mentioned, expected):
    channel = member_channel((ANA, level, muted))
    mentions = {ANA} if mentioned else set()

    assert push_recipients(AUTHOR, mentions, channel) == ([ANA] if expected else [])


def test_the_author_never_gets_a_push():
    assert push_recipients(AUTHOR, {AUTHOR}, member_channel()) == []


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
def test_a_read_only_channel_gets_no_pushes(flags):
    assert push_recipients(AUTHOR, set(), member_channel((ANA, "all", False), **flags)) == []


# PushService.handle


async def test_sends_to_every_browser_of_a_member_with_the_contract_payload():
    channel = member_channel((ANA, "all", False))
    fakes = Fakes(channel, subscription(ANA), subscription(ANA))
    event = created(channel, body="x" * 300)

    await fakes.service.handle(event)

    assert fakes.pushed == [ANA, ANA]
    data = json.loads(fakes.sender.sent[0][1])
    assert data == {
        "type": "message",
        "channel_id": str(channel.channel_id),
        "message_id": event.payload["message"]["id"],
        "seq": 7,
        "mentioned": False,
        "preview": "x" * PREVIEW_CHARS,
    }


async def test_mentioned_is_true_only_for_the_mentioned_user():
    channel = member_channel((ANA, "all", False), (BOB, "all", False))
    fakes = Fakes(channel, subscription(ANA), subscription(BOB))

    await fakes.service.handle(created(channel, mentions=(BOB,)))

    mentioned = {user: json.loads(data)["mentioned"] for user, data in fakes.sender.sent}
    assert mentioned == {ANA: False, BOB: True}


async def test_a_message_without_body_has_an_empty_preview():
    channel = member_channel((ANA, "all", False))
    fakes = Fakes(channel, subscription(ANA))

    await fakes.service.handle(created(channel, body=None))

    assert json.loads(fakes.sender.sent[0][1])["preview"] == ""


async def test_a_redelivered_event_pushes_nothing_new():
    channel = member_channel((ANA, "all", False))
    fakes, event = Fakes(channel, subscription(ANA)), created(channel)

    await fakes.service.handle(event)
    await fakes.service.handle(event)

    assert fakes.pushed == [ANA]


async def test_skips_users_with_a_tab_on_screen():
    channel = member_channel((ANA, "all", False), (BOB, "all", False))
    fakes = Fakes(channel, subscription(ANA), subscription(BOB))
    fakes.presence.looking.add(ANA)

    await fakes.service.handle(created(channel))

    assert fakes.pushed == [BOB]


async def test_users_without_subscriptions_are_skipped():
    channel = member_channel((ANA, "all", False), (BOB, "all", False))
    fakes = Fakes(channel, subscription(BOB))

    await fakes.service.handle(created(channel))

    assert fakes.pushed == [BOB]


async def test_a_gone_browser_is_deleted_and_the_others_still_get_it():
    channel = member_channel((ANA, "all", False))
    dead, alive = subscription(ANA), subscription(ANA)
    fakes = Fakes(channel, dead, alive, gone=(dead.endpoint,))

    await fakes.service.handle(created(channel))

    assert fakes.pushed == [ANA, ANA]
    assert fakes.subscriptions.deleted == [dead.endpoint]
    assert fakes.log == ["commit", "commit"]  # the read, then the delete


async def test_nothing_is_deleted_when_every_browser_answers():
    channel = member_channel((ANA, "all", False))
    fakes = Fakes(channel, subscription(ANA))

    await fakes.service.handle(created(channel))

    assert fakes.subscriptions.deleted == []
    assert fakes.log == ["commit"]


@pytest.mark.parametrize(
    "event_kwargs",
    [
        {"age": timedelta(minutes=11)},
        {"event_type": "reaction.added"},
        {"message": {"id": "not-a-uuid"}},
    ],
)
async def test_old_other_and_malformed_events_are_ignored(event_kwargs):
    channel = member_channel((ANA, "all", False))
    fakes = Fakes(channel, subscription(ANA))

    await fakes.service.handle(created(channel, **event_kwargs))

    assert fakes.sender.sent == []


async def test_a_deleted_channel_is_ignored():
    channel = member_channel((ANA, "all", False))
    fakes = Fakes(None, subscription(ANA))

    await fakes.service.handle(created(channel))

    assert fakes.sender.sent == []


async def test_core_down_raises_before_anything_is_sent():
    channel = member_channel((ANA, "all", False))
    fakes = Fakes(channel, subscription(ANA), core_error=ServiceUnavailable())

    with pytest.raises(ServiceUnavailable):
        await fakes.service.handle(created(channel))
    assert fakes.sender.sent == []
