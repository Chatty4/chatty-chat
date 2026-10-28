import json
import uuid
from collections.abc import AsyncGenerator
from fnmatch import fnmatch
from typing import cast

import pytest
from redis.asyncio import Redis

from app.clients.core import ALL_VERSION, CHANNEL_KEYS, channel_key, members_key, version_key
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
    """Cached keys in `data`, version counters in `versions`, the order of commands in `log`."""

    def __init__(self, *keys: str) -> None:
        self.data = dict.fromkeys(keys, "{}")
        self.versions: dict[str, int] = {}
        self.log: list[str] = []
        self.delete_sizes: list[int] = []

    async def delete(self, *keys: str) -> int:
        self.log.append("delete")
        self.delete_sizes.append(len(keys))
        return sum(self.data.pop(key, None) is not None for key in keys)

    async def incr(self, key: str) -> int:
        self.log.append("incr")
        self.versions[key] = self.versions.get(key, 0) + 1
        return self.versions[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def scan_iter(self, match: str, count: int | None = None) -> AsyncGenerator[str]:
        for key in list(self.data):
            if fnmatch(key, match):
                yield key

    @property
    def as_redis(self) -> Redis:
        """The fake only duck-types Redis, so cast it for the type checker."""
        return cast(Redis, self)


class FakePipeline:
    """Queues commands and runs them in order on execute(), like MULTI/EXEC."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incr(self, key: str) -> None:
        self._queued.append(("incr", (key,)))

    def expire(self, key: str, seconds: int) -> None:
        self._queued.append(("expire", (key, seconds)))

    def delete(self, *keys: str) -> None:
        self._queued.append(("delete", keys))

    async def execute(self) -> None:
        for name, args in self._queued:
            await getattr(self._redis, name)(*args)


def event(event_type: str, data: dict) -> str:
    envelope = {"type": event_type, "occurred_at": "2026-10-22T12:00:00Z", "data": data}
    return json.dumps(envelope, default=str)


MEMBERSHIP_CHANGED = (
    "membership.changed",
    {"user_id": USER_ID, "channel_id": CHANNEL_ID, "team_id": TEAM_ID, "action": "removed"},
)
TEAM_MEMBER_CHANGED = (
    "team.member.changed",
    {"user_id": USER_ID, "team_id": TEAM_ID, "action": "removed", "role": None},
)
CHANNEL_UPDATED = (
    "channel.updated",
    {"channel_id": CHANNEL_ID, "team_id": TEAM_ID, "name": "x", "topic": None, "archived": True},
)
TEAM_DELETED = ("team.deleted", {"team_id": TEAM_ID})
USER_DEACTIVATED = ("user.deactivated", {"user_id": USER_ID})


@pytest.mark.parametrize(
    ("event_type", "data", "deleted"),
    [
        (*MEMBERSHIP_CHANGED, {members_key(USER_ID), channel_key(CHANNEL_ID)}),
        (*TEAM_MEMBER_CHANGED, {members_key(USER_ID)}),
        (*CHANNEL_UPDATED, {channel_key(CHANNEL_ID)}),
        (*TEAM_DELETED, {channel_key(CHANNEL_ID), channel_key(OTHER_CHANNEL_ID)}),
        (*USER_DEACTIVATED, {members_key(USER_ID)}),
    ],
)
async def test_event_deletes_exactly_its_keys(event_type, data, deleted):
    redis = FakeRedis(*CACHED)

    await handle_event(redis.as_redis, event(event_type, data))

    assert set(redis.data) == CACHED - deleted


@pytest.mark.parametrize(
    ("event_type", "data", "bumped"),
    [
        (
            *MEMBERSHIP_CHANGED,
            {version_key(members_key(USER_ID)), version_key(channel_key(CHANNEL_ID))},
        ),
        (*TEAM_MEMBER_CHANGED, {version_key(members_key(USER_ID))}),
        (*CHANNEL_UPDATED, {version_key(channel_key(CHANNEL_ID))}),
        (*TEAM_DELETED, {ALL_VERSION}),
        (*USER_DEACTIVATED, {version_key(members_key(USER_ID))}),
    ],
)
async def test_event_bumps_exactly_its_versions(event_type, data, bumped):
    redis = FakeRedis(*CACHED)

    await handle_event(redis.as_redis, event(event_type, data))

    assert redis.versions == dict.fromkeys(bumped, 1)


async def test_version_is_bumped_before_the_key_is_deleted():
    redis = FakeRedis(*CACHED)

    await handle_event(redis.as_redis, event(*USER_DEACTIVATED))

    assert redis.log == ["incr", "delete"]


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
    assert redis.versions == {}
    assert "Ignoring" in caplog.text


async def test_clear_cache_deletes_only_members_and_channel_keys():
    redis = FakeRedis(*CACHED, "other:1")

    await clear_cache(redis.as_redis)

    assert list(redis.data) == ["other:1"]


async def test_clear_cache_bumps_the_global_version_first():
    redis = FakeRedis(*CACHED)

    await clear_cache(redis.as_redis)

    assert redis.versions == {ALL_VERSION: 1}
    assert redis.log[0] == "incr"


async def test_delete_matching_deletes_in_batches_of_500():
    keys = [channel_key(uuid.uuid4()) for _ in range(1201)]
    redis = FakeRedis(*keys, members_key(USER_ID))

    await delete_matching(redis.as_redis, CHANNEL_KEYS)

    assert redis.delete_sizes == [500, 500, 201]
    assert list(redis.data) == [members_key(USER_ID)]  # other keys stay
