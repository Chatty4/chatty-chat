import asyncio
import logging
import uuid
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from app.clients.core_events import CORE_EVENTS, MAX_BACKOFF_SECONDS
from app.clients.presence import PRESENCE_CHANNEL_PREFIX, PresenceStore, presence_channel
from app.core.codes import CloseCode
from app.schemas.core import Memberships
from app.schemas.core_events import (
    ChannelUpdatedData,
    CoreEvent,
    CoreEventType,
    MembershipAction,
    MembershipChangedData,
    TeamMemberAction,
    TeamMemberChangedData,
    UserData,
)
from app.schemas.events import ChannelEvent
from app.schemas.presence import PresenceFrame
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import channel_name

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 5


class Socket(Protocol):
    async def send_json(self, data: Any) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class Watched(NamedTuple):
    channel_id: uuid.UUID
    team_id: uuid.UUID


class Gateway:
    """One Redis subscription per process. Fans channel and core events out to local sockets."""

    def __init__(
        self, redis: Redis, connections: ConnectionRegistry[Socket], presence: PresenceStore
    ) -> None:
        self._redis = redis
        self._connections = connections
        self._presence = presence
        self._pubsub: PubSub | None = None  # None while Redis is down; run() fixes it
        self._watching: dict[Socket, Watched] = {}  # at most one watched channel per socket
        self._socket_ids: dict[Socket, str] = {}  # the socket's line in presence:{user_id}
        self._stuck: set[Socket] = set()  # closed as too_slow, waiting for their disconnect()
        self._closing: set[asyncio.Task] = set()  # keeps background closes from being collected

    async def connect(
        self,
        user_id: uuid.UUID,
        socket: Socket,
        channel_ids: list[uuid.UUID],
        team_ids: Sequence[uuid.UUID] = (),
    ) -> None:
        """Adds the socket to its channels and teams. The user's first socket sends online."""
        self._connections.add(user_id, socket)
        first = [cid for cid in channel_ids if self._connections.channels.join(socket, cid)]
        await self._subscribe(first)
        first_teams = [tid for tid in team_ids if self._connections.teams.join(socket, tid)]
        await self._subscribe_teams(first_teams)
        self._socket_ids[socket] = uuid.uuid4().hex

        if await self._presence.socket_opened(user_id, self._socket_ids[socket]):
            await self._presence.go_online(user_id, team_ids)

    async def heartbeat(self, user_id: uuid.UUID, socket: Socket) -> None:
        await self._presence.heartbeat(user_id, self._socket_ids[socket])

    async def disconnect(self, user_id: uuid.UUID, socket: Socket) -> None:
        """
        Removes the socket from its channels and teams. The user's last socket, on any gateway,
        sends offline.
        """
        self._watching.pop(socket, None)
        self._stuck.discard(socket)
        self._connections.remove(user_id, socket)
        await self._unsubscribe(self._connections.channels.leave_all(socket))
        team_ids = self._connections.teams.of(socket)
        await self._unsubscribe_teams(self._connections.teams.leave_all(socket))

        if await self._presence.socket_closed(user_id, self._socket_ids.pop(socket)):
            await self._presence.go_offline(user_id, team_ids)

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
        if socket in self._connections.channels.members(channel_id):
            return True  # already a member: its events arrive anyway, and unwatch mustn't leave
        await self.unwatch(socket)  # one per socket: the new watch replaces the old one
        self._watching[socket] = Watched(channel_id, team_id)
        if self._connections.channels.join(socket, channel_id):
            await self._subscribe([channel_id])
        return True

    async def unwatch(self, socket: Socket, channel_id: uuid.UUID | None = None) -> None:
        """Ends the socket's watch. With channel_id, only if it is that channel."""
        watched = self._watching.get(socket)
        if watched is None or channel_id not in (None, watched.channel_id):
            return
        del self._watching[socket]
        if self._connections.channels.leave(socket, watched.channel_id):
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
                    await self._subscribe(self._connections.channels.ids())
                    await self._subscribe_teams(self._connections.teams.ids())
                    backoff = 1
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        if message["channel"] == CORE_EVENTS:
                            await self.handle_core_event(message["data"])
                        elif message["channel"].startswith(PRESENCE_CHANNEL_PREFIX):
                            await self.handle_presence(message["data"])
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
        await self._send_all(self._connections.channels.members(event.channel_id), frame)

    async def handle_presence(self, raw: str) -> None:
        try:
            frame = PresenceFrame.model_validate_json(raw)
        except ValidationError:
            logger.warning("Ignoring malformed presence frame: %.200s", raw)
            return
        sockets = self._connections.teams.members(frame.team_id) - self._stuck
        await self._send_all(sockets, frame.model_dump(mode="json"))

    async def handle_core_event(self, raw: str) -> None:
        try:
            event = CoreEvent.model_validate_json(raw)
            match event.type:
                case CoreEventType.MEMBERSHIP_CHANGED:
                    await self._membership_changed(MembershipChangedData.model_validate(event.data))
                case CoreEventType.CHANNEL_UPDATED:
                    data = ChannelUpdatedData.model_validate(event.data)
                    # the WebSocket message type (api-chat.md), not the core event type
                    frame = {"type": "channel.updated"} | data.model_dump(mode="json")
                    await self._send_all(self._connections.channels.members(data.channel_id), frame)
                case CoreEventType.USER_DEACTIVATED:
                    data = UserData.model_validate(event.data)
                    for socket in list(self._connections.sockets(data.user_id)):
                        await self._close(socket, CloseCode.USER_INACTIVE, "user_inactive")
                case CoreEventType.TEAM_MEMBER_CHANGED:
                    await self._team_member_changed(
                        TeamMemberChangedData.model_validate(event.data)
                    )
                # other types: nothing for the gateway; the cache listener logs unknown ones
        except ValidationError:
            logger.warning("Ignoring malformed core event: %.200s", raw)

    async def _membership_changed(self, data: MembershipChangedData) -> None:
        sockets = list(self._connections.sockets(data.user_id))
        if data.action == MembershipAction.ADDED:
            first = [self._connections.channels.join(socket, data.channel_id) for socket in sockets]
            if any(first):
                await self._subscribe([data.channel_id])
            for socket in sockets:
                watched = self._watching.get(socket)
                if watched is not None and watched.channel_id == data.channel_id:
                    del self._watching[socket]  # the watch becomes a normal subscription
        elif data.action == MembershipAction.REMOVED:
            last = [self._connections.channels.leave(socket, data.channel_id) for socket in sockets]
            if any(last):
                await self._unsubscribe([data.channel_id])

    async def _team_member_changed(self, data: TeamMemberChangedData) -> None:
        sockets = list(self._connections.sockets(data.user_id))
        if not sockets:
            return
        if data.action == TeamMemberAction.JOINED:
            first = [self._connections.teams.join(socket, data.team_id) for socket in sockets]
            if any(first):
                await self._subscribe_teams([data.team_id])
            await self._presence.go_online(data.user_id, [data.team_id])
        elif data.action in (TeamMemberAction.LEFT, TeamMemberAction.REMOVED):
            for socket in sockets:
                watched = self._watching.get(socket)
                if watched is not None and watched.team_id == data.team_id:
                    await self.unwatch(socket)
            last = [self._connections.teams.leave(socket, data.team_id) for socket in sockets]
            if any(last):
                await self._unsubscribe_teams([data.team_id])
            await self._presence.go_offline(data.user_id, [data.team_id])

    async def _send_all(self, sockets: set[Socket], frame: dict[str, Any]) -> None:
        # shortcut: a stuck socket delays this gateway's events once, by SEND_TIMEOUT_SECONDS,
        # then it is dropped; give each socket its own queue if the load test (CHAT-224) shows it.
        await asyncio.gather(*(self._send(socket, frame) for socket in list(sockets)))

    async def _send(self, socket: Socket, frame: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(socket.send_json(frame), SEND_TIMEOUT_SECONDS)
        except TimeoutError:
            # The client stopped reading: stop sending to it, and close it so it reconnects and
            # /syncs what it missed. In the background, since it won't answer the close either.
            await self._unsubscribe(self._connections.channels.leave_all(socket))
            # It stays in its teams, because disconnect() needs them to send offline.
            self._stuck.add(socket)
            task = asyncio.create_task(self._close(socket, CloseCode.GOING_AWAY, "too_slow"))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        except Exception:  # the socket closed meanwhile; its disconnect() cleans up
            logger.debug("Dropped a frame for a closed socket")

    async def _close_all(self, code: int, reason: str) -> None:
        sockets = self._connections.all_sockets()
        await asyncio.gather(*(self._close(socket, code, reason) for socket in sockets))

    async def _close(self, socket: Socket, code: int, reason: str) -> None:
        try:
            # A stuck socket can't take the close frame either; don't wait on it forever.
            await asyncio.wait_for(socket.close(code, reason), SEND_TIMEOUT_SECONDS)
        except Exception:  # already closing; its disconnect() cleans up
            logger.debug("Socket was already closed")

    async def _subscribe(self, channel_ids: list[uuid.UUID]) -> None:
        await self._listen(list(map(channel_name, channel_ids)))

    async def _unsubscribe(self, channel_ids: list[uuid.UUID]) -> None:
        await self._stop_listening(list(map(channel_name, channel_ids)))

    async def _subscribe_teams(self, team_ids: list[uuid.UUID]) -> None:
        await self._listen(list(map(presence_channel, team_ids)))

    async def _unsubscribe_teams(self, team_ids: list[uuid.UUID]) -> None:
        await self._stop_listening(list(map(presence_channel, team_ids)))

    async def _listen(self, names: list[str]) -> None:
        if not names or self._pubsub is None:
            return
        try:
            await self._pubsub.subscribe(*names)
        except RedisError:
            logger.warning("Subscribe failed; run() subscribes again after the reconnect")

    async def _stop_listening(self, names: list[str]) -> None:
        if not names or self._pubsub is None:
            return
        try:
            await self._pubsub.unsubscribe(*names)
        except RedisError:
            logger.warning("Unsubscribe failed; the reconnect drops it anyway")
