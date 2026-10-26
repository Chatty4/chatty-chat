import asyncio
import logging
import uuid
from typing import Any, Protocol

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from app.clients.core_events import CORE_EVENTS, MAX_BACKOFF_SECONDS
from app.schemas.events import ChannelEvent
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import channel_name

logger = logging.getLogger(__name__)


class Socket(Protocol):
    async def send_json(self, data: Any) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class Gateway:
    """One Redis subscription per process. Fans channel and core events out to local sockets."""

    def __init__(self, redis: Redis, connections: ConnectionRegistry[Socket]) -> None:
        self._redis = redis
        self._connections = connections
        self._pubsub: PubSub | None = None  # None while Redis is down; run() fixes it

    async def connect(
        self, user_id: uuid.UUID, socket: Socket, channel_ids: list[uuid.UUID]
    ) -> None:
        self._connections.add(user_id, socket)
        first = [cid for cid in channel_ids if self._connections.join(socket, cid)]
        await self._subscribe(first)

    async def disconnect(self, user_id: uuid.UUID, socket: Socket) -> None:
        self._connections.remove(user_id, socket)
        await self._unsubscribe(self._connections.leave_all(socket))

    async def run(self) -> None:
        """Runs until cancelled. After a Redis drop, subscribes again to what the registry has."""
        backoff = 1
        while True:
            try:
                async with self._redis.pubsub() as pubsub:  # type: ignore
                    await pubsub.subscribe(CORE_EVENTS)  # opens the connection
                    self._pubsub = pubsub
                    await self._subscribe(self._connections.channel_ids())
                    backoff = 1
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        if message["channel"] == CORE_EVENTS:
                            await self.handle_core_event(message["data"])  # step 4
                        else:
                            await self.handle_channel_event(message["data"])
            except RedisError:
                logger.warning("Gateway lost Redis, retrying in %s s", backoff, exc_info=True)
                self._pubsub = None
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
        pass  # step 4

    async def _send_all(self, sockets: set[Socket], frame: dict[str, Any]) -> None:
        # shortcut: a slow socket delays the next event for everyone on this gateway;
        # give each socket its own queue if the load test (CHAT-224) shows it.
        await asyncio.gather(*(self._send(socket, frame) for socket in list(sockets)))

    async def _send(self, socket: Socket, frame: dict[str, Any]) -> None:
        try:
            await socket.send_json(frame)
        except Exception:  # the socket closed meanwhile; its disconnect() cleans up
            logger.debug("Dropped a frame for a closed socket")

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
