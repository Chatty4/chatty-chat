import json
import uuid
from collections.abc import AsyncGenerator
from fnmatch import fnmatch
from typing import cast

import pytest
from redis.asyncio import Redis

from app.clients.core import CHANNEL_KEYS, channel_key, members_key
from app.clients.core_events import clear_cache, delete_matching, handle_event

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
        self.delete_sizes: list[int] = []

    async def delete(self, *keys: str) -> int:
        self.delete_sizes.append(len(keys))
        return sum(self.data.pop(key, None) is not None for key in keys)

    async def scan_iter(self, match: str, count: int | None = None) -> AsyncGenerator[str]:
        for key in list(self.data):
            if fnmatch(key, match):
                yield key

    @property
    def as_redis(self) -> Redis:
        """The fake only duck-types Redis, so cast it for the type checker."""
        return cast(Redis, self)


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

    await handle_event(redis.as_redis, event(event_type, data))

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

    await handle_event(redis.as_redis, raw)

    assert set(redis.data) == CACHED
    assert "Ignoring" in caplog.text


async def test_clear_cache_deletes_only_members_and_channel_keys():
    redis = FakeRedis(*CACHED, "other:1")

    await clear_cache(redis.as_redis)

    assert list(redis.data) == ["other:1"]


async def test_delete_matching_deletes_in_batches_of_500():
    keys = [channel_key(uuid.uuid4()) for _ in range(1201)]
    redis = FakeRedis(*keys, members_key(USER_ID))

    await delete_matching(redis.as_redis, CHANNEL_KEYS)

    assert redis.delete_sizes == [500, 500, 201]
    assert list(redis.data) == [members_key(USER_ID)]  # other keys stay
