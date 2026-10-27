import uuid
from collections import defaultdict


class ConnectionRegistry[T]:
    """Open sockets of this process, by user. CHAT-184 and CHAT-185 send through it."""

    def __init__(self) -> None:
        self._sockets: defaultdict[uuid.UUID, set[T]] = defaultdict(set)  # user -> sockets
        self._channels: defaultdict[uuid.UUID, set[T]] = defaultdict(set)  # channel -> sockets
        self._joined: defaultdict[T, set[uuid.UUID]] = defaultdict(set)  # socket -> channels

    def add(self, user_id: uuid.UUID, socket: T) -> None:
        self._sockets[user_id].add(socket)

    def remove(self, user_id: uuid.UUID, socket: T) -> None:
        sockets = self._sockets.get(user_id)
        if sockets is None:
            return
        sockets.discard(socket)
        if not sockets:
            del self._sockets[user_id]

    def all_sockets(self) -> list[T]:
        return [socket for sockets in self._sockets.values() for socket in sockets]

    def sockets(self, user_id: uuid.UUID) -> set[T]:
        return self._sockets.get(user_id, set())

    def join(self, socket: T, channel_id: uuid.UUID) -> bool:
        """True when this is the channel's first socket: subscribe in Redis"""
        first = channel_id not in self._channels
        self._channels[channel_id].add(socket)
        self._joined[socket].add(channel_id)
        return first

    def leave(self, socket: T, channel_id: uuid.UUID) -> bool:
        """True when this is the channel's last socket: unsubscribe in Redis."""
        sockets = self._channels.get(channel_id)
        if sockets is None or socket not in sockets:
            return False
        sockets.discard(socket)
        self._joined[socket].discard(channel_id)
        if sockets:
            return False
        del self._channels[channel_id]
        return True

    def leave_all(self, socket: T) -> list[uuid.UUID]:
        """On disconnect. Returns the channels that are now empty."""
        emptied = []
        for channel_id in self._joined.pop(socket, set()):
            sockets = self._channels[channel_id]
            sockets.discard(socket)
            if not sockets:
                del self._channels[channel_id]
                emptied.append(channel_id)
        return emptied

    def in_channel(self, channel_id: uuid.UUID) -> set[T]:
        return self._channels.get(channel_id, set())

    def channel_ids(self) -> list[uuid.UUID]:
        return list(self._channels)
