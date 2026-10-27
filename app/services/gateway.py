import asyncio
import logging
import uuid
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from app.clients.core_events import CORE_EVENTS, MAX_BACKOFF_SECONDS
from app.core.codes import CloseCode
from app.schemas.core import Memberships
from app.schemas.core_events import (
    ChannelUpdatedData,
    CoreEvent,
    MembershipAction,
    MembershipChangedData,
    TeamMemberAction,
    TeamMemberChangedData,
    UserData,
)
from app.schemas.events import ChannelEvent
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import channel_name

logger = logging.getLogger(__name__)


class Socket(Protocol):
    async def send_json(self, data: Any) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class Watched(NamedTuple):
    channel_id: uuid.UUID
    team_id: uuid.UUID


class Gateway:
    """One Redis subscription per process. Fans channel and core events out to local sockets."""

    def __init__(self, redis: Redis, connections: ConnectionRegistry[Socket]) -> None:
        self._redis = redis
        self._connections = connections
        self._pubsub: PubSub | None = None  # None while Redis is down; run() fixes it
        self._watching: dict[Socket, Watched] = {}  # at most one watched channel per socket

    async def connect(
        self, user_id: uuid.UUID, socket: Socket, channel_ids: list[uuid.UUID]
    ) -> None:
        self._connections.add(user_id, socket)
        first = [cid for cid in channel_ids if self._connections.join(socket, cid)]
        await self._subscribe(first)

    async def disconnect(self, user_id: uuid.UUID, socket: Socket) -> None:
        self._watching.pop(socket, None)
        self._connections.remove(user_id, socket)
        await self._unsubscribe(self._connections.leave_all(socket))

    async def watch(self, socket: Socket, user: Memberships, channel_id: uuid.UUID) -> bool:
        """False when the channel isn't public in one of the user's teams (cannot_watch)."""
        # shortcut: user is the memberships loaded at connect, so someone removed from a team
        # can still start a watch on its channels until they reconnect; reload them if it matters.
        team_id = next(
            (team.team_id for team in user.teams if channel_id in team.public_channel_ids), None
        )
        if team_id is None:
            return False
        watched = self._watching.get(socket)
        if watched is not None and watched.channel_id == channel_id:
            return True  # already watching it
        if socket in self._connections.in_channel(channel_id):
            return True  # already a member: its events arrive anyway, and unwatch mustn't leave
        await self.unwatch(socket)  # one per socket: the new watch replaces the old one
        self._watching[socket] = Watched(channel_id, team_id)
        if self._connections.join(socket, channel_id):
            await self._subscribe([channel_id])
        return True

    async def unwatch(self, socket: Socket, channel_id: uuid.UUID | None = None) -> None:
        """Ends the socket's watch. With channel_id, only if it is that channel."""
        watched = self._watching.get(socket)
        if watched is None or channel_id not in (None, watched.channel_id):
            return
        del self._watching[socket]
        if self._connections.leave(socket, watched.channel_id):
            await self._unsubscribe([watched.channel_id])

    async def run(self) -> None:
        """Runs until cancelled. After a Redis drop, closes every socket so clients /sync."""
        backoff = 1
        reconnect = False
        while True:
            try:
                async with self._redis.pubsub() as pubsub:  # type: ignore
                    await pubsub.subscribe(CORE_EVENTS)  # opens the connection
                    self._pubsub = pubsub
                    if reconnect:
                        # Pub/sub doesn't replay what was sent while Redis was down.
                        await self._close_all(CloseCode.GOING_AWAY, "gateway_restart")
                    await self._subscribe(self._connections.channel_ids())
                    backoff = 1
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        if message["channel"] == CORE_EVENTS:
                            await self.handle_core_event(message["data"])
                        else:
                            await self.handle_channel_event(message["data"])
            except RedisError:
                logger.warning("Gateway lost Redis, retrying in %s s", backoff, exc_info=True)
                self._pubsub = None
                reconnect = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def handle_channel_event(self, raw: str) -> None:
        try:
            event = ChannelEvent.model_validate_json(raw)
        except ValidationError:
            logger.warning("Ignoring malformed channel event: %.200s", raw)
            return
        frame = {"type": "event", "event": event.model_dump(mode="json")}
        await self._send_all(self._connections.in_channel(event.channel_id), frame)

    async def handle_core_event(self, raw: str) -> None:
        try:
            event = CoreEvent.model_validate_json(raw)
            match event.type:
                case "membership.changed":
                    await self._membership_changed(MembershipChangedData.model_validate(event.data))
                case "channel.updated":
                    data = ChannelUpdatedData.model_validate(event.data)
                    frame = {"type": "channel.updated"} | data.model_dump(mode="json")
                    await self._send_all(self._connections.in_channel(data.channel_id), frame)
                case "user.deactivated":
                    data = UserData.model_validate(event.data)
                    for socket in list(self._connections.sockets(data.user_id)):
                        await self._close(socket, CloseCode.USER_INACTIVE, "user_inactive")
                case "team.member.changed":
                    data = TeamMemberChangedData.model_validate(event.data)
                    if data.action in (TeamMemberAction.LEFT, TeamMemberAction.REMOVED):
                        for socket in list(self._connections.sockets(data.user_id)):
                            watched = self._watching.get(socket)
                            if watched is not None and watched.team_id == data.team_id:
                                await self.unwatch(socket)
                # other types: nothing for the gateway; the cache listener logs unknown ones
        except ValidationError:
            logger.warning("Ignoring malformed core event: %.200s", raw)

    async def _membership_changed(self, data: MembershipChangedData) -> None:
        sockets = list(self._connections.sockets(data.user_id))
        if data.action == MembershipAction.ADDED:
            first = [self._connections.join(socket, data.channel_id) for socket in sockets]
            if any(first):
                await self._subscribe([data.channel_id])
            for socket in sockets:
                watched = self._watching.get(socket)
                if watched is not None and watched.channel_id == data.channel_id:
                    del self._watching[socket]  # the watch becomes a normal subscription
        elif data.action == MembershipAction.REMOVED:
            last = [self._connections.leave(socket, data.channel_id) for socket in sockets]
            if any(last):
                await self._unsubscribe([data.channel_id])

    async def _send_all(self, sockets: set[Socket], frame: dict[str, Any]) -> None:
        # shortcut: a slow socket delays the next event for everyone on this gateway;
        # give each socket its own queue if the load test (CHAT-224) shows it.
        await asyncio.gather(*(self._send(socket, frame) for socket in list(sockets)))

    async def _send(self, socket: Socket, frame: dict[str, Any]) -> None:
        try:
            await socket.send_json(frame)
        except Exception:  # the socket closed meanwhile; its disconnect() cleans up
            logger.debug("Dropped a frame for a closed socket")

    async def _close_all(self, code: int, reason: str) -> None:
        sockets = self._connections.all_sockets()
        await asyncio.gather(*(self._close(socket, code, reason) for socket in sockets))

    async def _close(self, socket: Socket, code: int, reason: str) -> None:
        try:
            await socket.close(code, reason)
        except Exception:  # already closing; its disconnect() cleans up
            logger.debug("Socket was already closed")

    async def _subscribe(self, channel_ids: list[uuid.UUID]) -> None:
        if not channel_ids or self._pubsub is None:
            return
        try:
            await self._pubsub.subscribe(*map(channel_name, channel_ids))
        except RedisError:
            logger.warning("Subscribe failed; run() subscribes again after the reconnect")

    async def _unsubscribe(self, channel_ids: list[uuid.UUID]) -> None:
        if not channel_ids or self._pubsub is None:
            return
        try:
            await self._pubsub.unsubscribe(*map(channel_name, channel_ids))
        except RedisError:
            logger.warning("Unsubscribe failed; the reconnect drops it anyway")
