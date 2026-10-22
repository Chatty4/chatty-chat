import uuid
from collections import defaultdict


class ConnectionRegistry[T]:
    """Open sockets of this process, by user. CHAT-184 and CHAT-185 send through it."""

    def __init__(self) -> None:
        self._sockets: defaultdict[uuid.UUID, set[T]] = defaultdict(set)

    def add(self, user_id: uuid.UUID, socket: T) -> None:
        self._sockets[user_id].add(socket)

    def remove(self, user_id: uuid.UUID, socket: T) -> None:
        sockets = self._sockets.get(user_id)
        if sockets is None:
            return
        sockets.discard(socket)
        if not sockets:
            del self._sockets[user_id]

    def sockets(self, user_id: uuid.UUID) -> set[T]:
        return self._sockets.get(user_id, set())
