import uuid
from collections import defaultdict


class SocketGroups[T]:
    """Which sockets are in which group. A group is a channel or a team, by its ID."""

    def __init__(self) -> None:
        self._sockets: defaultdict[uuid.UUID, set[T]] = defaultdict(set)  # group -> sockets
        self._groups: defaultdict[T, set[uuid.UUID]] = defaultdict(set)  # socket -> groups

    def join(self, socket: T, group_id: uuid.UUID) -> bool:
        """True when this is the group's first socket: subscribe in Redis."""
        first = group_id not in self._sockets
        self._sockets[group_id].add(socket)
        self._groups[socket].add(group_id)
        return first

    def leave(self, socket: T, group_id: uuid.UUID) -> bool:
        """True when this is the group's last socket: unsubscribe in Redis."""
        sockets = self._sockets.get(group_id)
        if sockets is None or socket not in sockets:
            return False
        sockets.discard(socket)
        self._groups[socket].discard(group_id)
        if sockets:
            return False
        del self._sockets[group_id]
        return True

    def leave_all(self, socket: T) -> list[uuid.UUID]:
        """On disconnect. Returns the groups that are now empty."""
        emptied = []
        for group_id in self._groups.pop(socket, set()):
            sockets = self._sockets[group_id]
            sockets.discard(socket)
            if not sockets:
                del self._sockets[group_id]
                emptied.append(group_id)
        return emptied

    def of(self, socket: T) -> list[uuid.UUID]:
        """The groups the socket is in."""
        return list(self._groups.get(socket, set()))

    def members(self, group_id: uuid.UUID) -> set[T]:
        return self._sockets.get(group_id, set())

    def ids(self) -> list[uuid.UUID]:
        return list(self._sockets)


class ConnectionRegistry[T]:
    """Open sockets of this process, by user, by channel and by team."""

    def __init__(self) -> None:
        self._sockets: defaultdict[uuid.UUID, set[T]] = defaultdict(set)  # user -> sockets
        self.channels: SocketGroups[T] = SocketGroups()
        self.teams: SocketGroups[T] = SocketGroups()

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
