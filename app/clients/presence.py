import logging
import time
import uuid
from collections.abc import Iterable
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.schemas.presence import PresenceFrame, PresenceStatus

logger = logging.getLogger(__name__)

PRESENCE_TTL_SECONDS = 60  # the same as the heartbeat timeout
VISIBLE_WINDOW_SECONDS = 45
PRESENCE_CHANNEL_PREFIX = "chat.presence:"


def presence_key(user_id: uuid.UUID) -> str:
    return f"presence:{user_id}"


def visible_key(user_id: uuid.UUID) -> str:
    """The user's sockets with a tab on screen, for the push worker (events.md, heartbeat)."""
    return f"visible:{user_id}"


def online_key(team_id: uuid.UUID) -> str:
    return f"online:{team_id}"


def presence_channel(team_id: uuid.UUID) -> str:
    return f"{PRESENCE_CHANNEL_PREFIX}{team_id}"


class PresenceStore:
    """
    Who is online, shared by every gateway. A Redis error is logged and never closes a socket.
    presence:{user_id} has one line per open socket: socket ID -> last heartbeat time. Each
    change runs as one transaction, so two sockets closing at once can't both be the last.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def socket_opened(self, user_id: uuid.UUID, socket_id: str) -> bool:
        """True when this is the user's first socket."""
        key = presence_key(user_id)
        now = time.time()
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, "-inf", now - PRESENCE_TTL_SECONDS)
                pipe.zcard(key)
                pipe.zadd(key, {socket_id: now})
                pipe.expire(key, PRESENCE_TTL_SECONDS)
                _, lines_before, _, _ = await pipe.execute()
        except RedisError:
            logger.warning("Presence update failed for user %s", user_id, exc_info=True)
            return False
        return lines_before == 0

    async def heartbeat(self, user_id: uuid.UUID, socket_id: str, visible: bool) -> None:
        """
        Refreshes the socket's line in presence:{user_id}, and adds it to
        visible:{user_id} or takes it out.
        """
        key, tabs = presence_key(user_id), visible_key(user_id)
        now = time.time()
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zadd(key, {socket_id: now})
                pipe.expire(key, PRESENCE_TTL_SECONDS)
                if visible:
                    # lines of crashed gateways would stay while another tab keeps the TTL fresh
                    pipe.zremrangebyscore(tabs, "-inf", now - PRESENCE_TTL_SECONDS)
                    pipe.zadd(tabs, {socket_id: now})
                    pipe.expire(tabs, PRESENCE_TTL_SECONDS)
                else:
                    pipe.zrem(tabs, socket_id)
                await pipe.execute()
        except RedisError:
            logger.warning("Presence update failed for user %s", user_id, exc_info=True)

    async def socket_closed(self, user_id: uuid.UUID, socket_id: str) -> bool:
        """True when this was the user's last socket. Also takes it out of visible:{user_id}."""
        key = presence_key(user_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zrem(key, socket_id)
                pipe.zremrangebyscore(key, "-inf", time.time() - PRESENCE_TTL_SECONDS)
                pipe.zcard(key)
                pipe.zrem(visible_key(user_id), socket_id)
                _, _, lines_left, _ = await pipe.execute()
        except RedisError:
            logger.warning("Presence update failed for user %s", user_id, exc_info=True)
            return False
        return lines_left == 0

    async def go_online(self, user_id: uuid.UUID, team_ids: Iterable[uuid.UUID]) -> None:
        for team_id in team_ids:
            try:
                await self._redis.sadd(online_key(team_id), str(user_id))  # type: ignore[misc]
                await self._publish(team_id, user_id, PresenceStatus.ONLINE)
            except RedisError:
                logger.warning("Presence update failed for team %s", team_id, exc_info=True)

    async def go_offline(self, user_id: uuid.UUID, team_ids: Iterable[uuid.UUID]) -> None:
        """Only the gateway that really removes the user sends offline."""
        for team_id in team_ids:
            try:
                if await self._redis.srem(online_key(team_id), str(user_id)):  # type: ignore[misc]
                    await self._publish(team_id, user_id, PresenceStatus.OFFLINE)
            except RedisError:
                logger.warning("Presence update failed for team %s", team_id, exc_info=True)

    async def list_online(self, team_id: uuid.UUID) -> list[uuid.UUID]:
        """
        The team's online users. Leaves out users whose key expired after a crash, without
        removing them from the set: that could race with the user reconnecting.
        """
        try:
            online = await self._redis.smembers(online_key(team_id))  # type: ignore[misc]
            members = cast(list[str], list(online))  # decode_responses: never bytes
            async with self._redis.pipeline(transaction=False) as pipe:
                for member in members:
                    pipe.exists(presence_key(uuid.UUID(member)))
                alive = await pipe.execute()
        except RedisError:
            logger.warning("Presence read failed for team %s", team_id, exc_info=True)
            return []
        return [uuid.UUID(m) for m, is_alive in zip(members, alive, strict=True) if is_alive]

    async def is_looking(self, user_id: uuid.UUID) -> bool:
        """
        True when a tab of the user was on screen in the last 45 s; the push worker then sends
        nothing. A socket of a crashed gateway stays in the set but stops counting. A Redis error
        gives False: an extra notification is better than a missed one.
        """
        try:
            count = await self._redis.zcount(
                visible_key(user_id), time.time() - VISIBLE_WINDOW_SECONDS, "+inf"
            )
        except RedisError:
            logger.warning("Visible read failed for user %s", user_id, exc_info=True)
            return False
        return count > 0

    async def _publish(
        self, team_id: uuid.UUID, user_id: uuid.UUID, status: PresenceStatus
    ) -> None:
        frame = PresenceFrame(team_id=team_id, user_id=user_id, status=status)
        await self._redis.publish(presence_channel(team_id), frame.model_dump_json())
