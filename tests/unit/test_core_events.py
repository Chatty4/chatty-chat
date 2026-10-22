import json
import uuid
from collections.abc import AsyncGenerator
from fnmatch import fnmatch

import pytest

from app.clients.core import channel_key, members_key
from app.clients.core_events import clear_cache, handle_event

USER_ID, OTHER_USER_ID = uuid.uuid4(), uuid.uuid4()
CHANNEL_ID, OTHER_CHANNEL_ID = uuid.uuid4(), uuid.uuid4()
TEAM_ID = uuid.uuid4()
CACHED = {
    members_key(USER_ID),
    members_key(OTHER_USER_ID),
    channel_key(CHANNEL_ID),
    channel_key(OTHER_CHANNEL_ID),
}


class FakeRedis:
    def __init__(self, *keys: str) -> None:
        self.data = dict.fromkeys(keys, "{}")

    async def delete(self, *keys: str) -> int:
        return sum(self.data.pop(key, None) is not None for key in keys)

    async def scan_iter(self, match: str, count: int | None = None) -> AsyncGenerator[str]:
        for key in list(self.data):
            if fnmatch(key, match):
                yield key


def event(event_type: str, data: dict) -> str:
    envelope = {"type": event_type, "occurred_at": "2026-10-22T12:00:00Z", "data": data}
    return json.dumps(envelope, default=str)


@pytest.mark.parametrize(
    ("event_type", "data", "deleted"),
    [
        (
            "membership.changed",
            {"user_id": USER_ID, "channel_id": CHANNEL_ID, "team_id": TEAM_ID, "action": "removed"},
            {members_key(USER_ID), channel_key(CHANNEL_ID)},
        ),
        (
            "team.member.changed",
            {"user_id": USER_ID, "team_id": TEAM_ID, "action": "removed", "role": None},
            {members_key(USER_ID)},
        ),
        (
            "channel.updated",
            {
                "channel_id": CHANNEL_ID,
                "team_id": TEAM_ID,
                "name": "x",
                "topic": None,
                "archived": True,
            },
            {channel_key(CHANNEL_ID)},
        ),
        (
            "team.deleted",
            {"team_id": TEAM_ID},
            {channel_key(CHANNEL_ID), channel_key(OTHER_CHANNEL_ID)},
        ),
        ("user.deactivated", {"user_id": USER_ID}, {members_key(USER_ID)}),
    ],
)
async def test_event_deletes_exactly_its_keys(event_type, data, deleted):
    redis = FakeRedis(*CACHED)

    await handle_event(redis, event(event_type, data))

    assert set(redis.data) == CACHED - deleted


@pytest.mark.parametrize(
    "raw",
    [
        event("team.renamed", {"team_id": TEAM_ID}),  # unknown type
        "not json",
        event("user.deactivated", {}),  # missing user_id
        '{"type": "user.deactivated", "data": null}',
    ],
)
async def test_unknown_or_malformed_event_is_logged_and_ignored(raw, caplog):
    redis = FakeRedis(*CACHED)

    await handle_event(redis, raw)

    assert set(redis.data) == CACHED
    assert "Ignoring" in caplog.text


async def test_clear_cache_deletes_only_members_and_channel_keys():
    redis = FakeRedis(*CACHED, "other:1")

    await clear_cache(redis)

    assert list(redis.data) == ["other:1"]
