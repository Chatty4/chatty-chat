import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.core import CoreClient
from app.models import ChannelEvent, Message, Pin
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    PinRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.repositories.read_state import UnreadCounts
from app.schemas.core import CoreChannel, Memberships, TeamMembership
from app.schemas.events import ChannelEvent as Envelope  # the model is ChannelEvent here
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService
from app.services.pin_service import PinService
from app.services.reaction_service import ReactionService
from app.services.read_state_service import ReadStateService
from app.services.sync_service import SyncService


def memberships(
    user_id: uuid.UUID, *, is_active: bool = True, teams: list[TeamMembership] | None = None
) -> Memberships:
    return Memberships(user_id=user_id, is_active=is_active, teams=teams or [], channels=[])


def channel(
    *members: uuid.UUID,
    owner: uuid.UUID | None = None,
    kind: str = "public",
    archived: bool = False,
    team_deleted: bool = False,
) -> CoreChannel:
    """`owner` is a member with role owner, added to `members` if it isn't there."""
    member_ids = list(members)
    if owner is not None and owner not in member_ids:
        member_ids.append(owner)
    return CoreChannel.model_validate(
        {
            "channel_id": uuid.uuid4(),
            "team_id": uuid.uuid4(),
            "kind": kind,
            "archived": archived,
            "team_deleted": team_deleted,
            "members": [
                {
                    "user_id": m,
                    "role": "owner" if m == owner else "member",
                    "notify_level": "mentions",
                    "muted": False,
                }
                for m in member_ids
            ],
        }
    )


class FakeCore:
    """Stands in for CoreClient: returns what it was given, or raises `error`."""

    def __init__(
        self,
        memberships: Memberships | None = None,
        channel: CoreChannel | None = None,
        error: Exception | None = None,
    ) -> None:
        self._memberships = memberships
        self._channel = channel
        self._error = error

    async def get_memberships(self, user_id: uuid.UUID) -> Memberships | None:
        if self._error:
            raise self._error
        return self._memberships

    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        if self._error:
            raise self._error
        return self._channel


def saved_message(
    channel_id: uuid.UUID, author_id: uuid.UUID, client_msg_id: uuid.UUID, seq: int = 1
) -> Message:
    """A message as it comes back from the database."""
    return Message(
        id=uuid.uuid4(),
        channel_id=channel_id,
        author_id=author_id,
        seq=seq,
        client_msg_id=client_msg_id,
        body="hi",
        mentions=[],
        edited_at=None,
        deleted_at=None,
        created_at=datetime.now(UTC),
    )


def saved_event(channel_id: uuid.UUID, seq: int) -> ChannelEvent:
    """A channel event row as it comes back from the database."""
    return ChannelEvent(
        channel_id=channel_id,
        seq=seq,
        type="message.created",
        payload={},
        created_at=datetime.now(UTC),
    )


class _FakeTransaction:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._log.append("rollback" if exc_type else "commit")
        return False


class FakeSession:
    """Only `begin()`, which writes commit or rollback to `log`."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._log)


class FakeMessages:
    """
    `existing` is what the retry lookup finds. With `insert_error`, insert_message raises it, as if
    another request had saved `saved_meanwhile` between the lookup and the insert.
    `stored` are the messages get_message finds. With `deleted_meanwhile`, update_message_body,
    soft_delete_message and lock_undeleted_message find nothing, as if the message was deleted
    after the service checked it.
    """

    def __init__(
        self,
        existing: Message | None = None,
        *,
        insert_error: Exception | None = None,
        saved_meanwhile: Message | None = None,
        stored: list[Message] | None = None,
        deleted_meanwhile: bool = False,
    ) -> None:
        self.existing = existing
        self._insert_error = insert_error
        self._saved_meanwhile = saved_meanwhile
        self._stored = {message.id: message for message in stored or []}
        self._deleted_meanwhile = deleted_meanwhile
        self.inserted: list[Message] = []

    async def get_message(self, message_id: uuid.UUID) -> Message | None:
        return self._stored.get(message_id)

    async def update_message_body(
        self, message_id: uuid.UUID, body: str, mentions: list[uuid.UUID]
    ) -> Message | None:
        message = self._stored.get(message_id)
        if message is None or message.deleted_at is not None or self._deleted_meanwhile:
            return None
        message.body = body
        message.mentions = mentions
        message.edited_at = datetime.now(UTC)
        return message

    async def soft_delete_message(self, message_id: uuid.UUID) -> Message | None:
        message = self._stored.get(message_id)
        if message is None or message.deleted_at is not None or self._deleted_meanwhile:
            return None
        message.body = ""
        message.mentions = []
        message.deleted_at = datetime.now(UTC)
        return message

    async def lock_undeleted_message(self, message_id: uuid.UUID) -> bool:
        message = self._stored.get(message_id)
        return message is not None and message.deleted_at is None and not self._deleted_meanwhile

    async def get_by_client_msg_id(
        self, author_id: uuid.UUID, client_msg_id: uuid.UUID
    ) -> Message | None:
        return self.existing

    async def insert_message(self, **fields: Any) -> Message:
        if self._insert_error is not None:
            self.existing = self._saved_meanwhile
            raise self._insert_error
        message = Message(
            id=uuid.uuid4(),
            edited_at=None,
            deleted_at=None,
            created_at=datetime.now(UTC),
            **fields,
        )
        self.inserted.append(message)
        return message

    async def list_messages_before(
        self, channel_id: uuid.UUID, before_seq: int | None, limit: int
    ) -> list[Message]:
        rows = [
            message
            for message in self._stored.values()
            if message.channel_id == channel_id and (before_seq is None or message.seq < before_seq)
        ]
        return sorted(rows, key=lambda message: message.seq, reverse=True)[:limit]


class FakeCounters:
    """`per_channel` gives get_last_seq a value per channel; otherwise it returns last_seq."""

    def __init__(self, last_seq: int = 0, per_channel: dict[uuid.UUID, int] | None = None) -> None:
        self.last_seq = last_seq
        self.per_channel = per_channel or {}

    async def next_seq(self, channel_id: uuid.UUID) -> int:
        self.last_seq += 1
        return self.last_seq

    async def get_last_seq(self, channel_id: uuid.UUID) -> int:
        return self.per_channel.get(channel_id, self.last_seq)


class FakeChannelEvents:
    """
    `rows` are the inserted events; `stored` are what list_events_after finds.
    `cleared` records each clear_message_text call as (channel_id, seq, message_id, was_edited).
    """

    def __init__(self, stored: list[ChannelEvent] | None = None) -> None:
        self.rows: list[tuple[uuid.UUID, int, str, dict[str, Any]]] = []
        self.stored = stored or []
        self.cleared: list[tuple[uuid.UUID, int, uuid.UUID, bool]] = []

    async def insert_channel_event(
        self, channel_id: uuid.UUID, seq: int, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.rows.append((channel_id, seq, event_type, payload))

    async def clear_message_text(
        self, channel_id: uuid.UUID, seq: int, message_id: uuid.UUID, was_edited: bool
    ) -> None:
        self.cleared.append((channel_id, seq, message_id, was_edited))

    async def list_events_after(
        self, channel_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[ChannelEvent]:
        rows = [e for e in self.stored if e.channel_id == channel_id and e.seq > after_seq]
        return sorted(rows, key=lambda event: event.seq)[:limit]


class FakeReadState:
    """
    `positions` are the read positions by (user_id, channel_id). count_unread returns `counts`
    per channel as (unread, mentions), or (0, 0); `counted` records each call.
    """

    def __init__(self, counts: dict[uuid.UUID, tuple[int, int]] | None = None) -> None:
        self.positions: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
        self.counts = counts or {}
        self.counted: list[tuple[uuid.UUID, list[uuid.UUID]]] = []

    async def advance_read_position(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, seq: int
    ) -> int:
        key = (user_id, channel_id)
        self.positions[key] = max(self.positions.get(key, 0), seq)
        return self.positions[key]

    async def count_unread(
        self, user_id: uuid.UUID, channel_ids: list[uuid.UUID]
    ) -> list[UnreadCounts]:
        self.counted.append((user_id, list(channel_ids)))
        return [UnreadCounts(c, *self.counts.get(c, (0, 0))) for c in channel_ids]


class FakeReactions:
    """
    `rows` are the stored reactions as (message_id, user_id, emoji), oldest first.
    `asked` records the message ids of each list_reactions call.
    """

    def __init__(self, rows: list[tuple[uuid.UUID, uuid.UUID, str]] | None = None) -> None:
        self.rows = list(rows or [])
        self.asked: list[list[uuid.UUID]] = []

    async def add_reaction(
        self, message_id: uuid.UUID, user_id: uuid.UUID, emoji: str
    ) -> datetime | None:
        if (message_id, user_id, emoji) in self.rows:
            return None
        self.rows.append((message_id, user_id, emoji))
        return datetime.now(UTC)

    async def remove_reaction(
        self, message_id: uuid.UUID, user_id: uuid.UUID, emoji: str
    ) -> datetime | None:
        if (message_id, user_id, emoji) not in self.rows:
            return None
        self.rows.remove((message_id, user_id, emoji))
        return datetime.now(UTC)

    async def delete_message_reactions(self, message_id: uuid.UUID) -> None:
        self.rows = [row for row in self.rows if row[0] != message_id]

    async def list_reactions(
        self, message_ids: list[uuid.UUID], user_id: uuid.UUID
    ) -> dict[uuid.UUID, list[tuple[str, int, bool]]]:
        self.asked.append(list(message_ids))
        users: dict[uuid.UUID, dict[str, list[uuid.UUID]]] = {}
        for message_id, row_user_id, emoji in self.rows:
            if message_id in message_ids:
                users.setdefault(message_id, {}).setdefault(emoji, []).append(row_user_id)
        return {
            message_id: [(emoji, len(ids), user_id in ids) for emoji, ids in per_emoji.items()]
            for message_id, per_emoji in users.items()
        }


def saved_pin(message: Message, pinned_by: uuid.UUID, pinned_at: datetime | None = None) -> Pin:
    """A pin as it comes back from the database."""
    return Pin(
        channel_id=message.channel_id,
        message_id=message.id,
        pinned_by=pinned_by,
        pinned_at=pinned_at or datetime.now(UTC),
    )


class FakePins:
    """`rows` are the stored pins by message id. list_pins finds their messages in `messages`."""

    def __init__(self, messages: FakeMessages | None = None, rows: list[Pin] | None = None) -> None:
        self._messages = messages
        self.rows = {pin.message_id: pin for pin in rows or []}

    async def get_pin(self, message_id: uuid.UUID) -> Pin | None:
        return self.rows.get(message_id)

    async def count_pins(self, channel_id: uuid.UUID) -> int:
        return sum(pin.channel_id == channel_id for pin in self.rows.values())

    async def add_pin(
        self, channel_id: uuid.UUID, message_id: uuid.UUID, user_id: uuid.UUID
    ) -> datetime:
        pin = Pin(channel_id=channel_id, message_id=message_id, pinned_by=user_id)
        pin.pinned_at = datetime.now(UTC)
        self.rows[message_id] = pin
        return pin.pinned_at

    async def remove_pin(self, message_id: uuid.UUID) -> datetime | None:
        return None if self.rows.pop(message_id, None) is None else datetime.now(UTC)

    async def delete_message_pin(self, message_id: uuid.UUID) -> None:
        self.rows.pop(message_id, None)

    async def pinned_message_ids(self, message_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        return set(message_ids) & self.rows.keys()

    async def list_pins(self, channel_id: uuid.UUID) -> list[tuple[Pin, Message]]:
        assert self._messages is not None
        pins = sorted(
            (pin for pin in self.rows.values() if pin.channel_id == channel_id),
            key=lambda pin: pin.pinned_at,
            reverse=True,
        )
        result = []
        for pin in pins:
            message = await self._messages.get_message(pin.message_id)
            assert message is not None
            result.append((pin, message))
        return result


class FakeBus:
    """
    Records what was published, and writes "publish" or "send_to_user" to `log` to show when.
    `sent` are the live messages for users, as (user_id, the JSON the gateway forwards).
    """

    def __init__(self, log: list[str] | None = None) -> None:
        self._log = log if log is not None else []
        self.published: list[tuple[uuid.UUID, dict[str, Any]]] = []
        self.sent: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def publish(self, event: Envelope) -> None:
        self._log.append("publish")
        self.published.append((event.channel_id, event.model_dump(mode="json")))

    async def send_to_user(self, user_id: uuid.UUID, message: BaseModel) -> None:
        self._log.append("send_to_user")
        self.sent.append((user_id, message.model_dump(mode="json")))


class MessageFakes:
    """A MessageService on fakes. `log` shows the order of commit, rollback and publish."""

    def __init__(self, core: FakeCore, messages: FakeMessages | None = None) -> None:
        self.log: list[str] = []
        self.messages = messages or FakeMessages()
        self.counters = FakeCounters()
        self.events = FakeChannelEvents()
        self.read_state = FakeReadState()
        self.reactions = FakeReactions()
        self.pins = FakePins()
        self.bus = FakeBus(self.log)
        # The fakes only duck-type the real classes, so cast them for the type checker.
        self.service = MessageService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(MessageRepository, self.messages),
            cast(ChannelCounterRepository, self.counters),
            cast(ChannelEventRepository, self.events),
            cast(ReadStateRepository, self.read_state),
            cast(ReactionRepository, self.reactions),
            cast(PinRepository, self.pins),
            AccessService(cast(CoreClient, core)),
            cast(EventBus, self.bus),
        )


class ReactionFakes:
    """A ReactionService on fakes. `log` shows the order of commit, rollback and publish."""

    def __init__(
        self, core: FakeCore, messages: FakeMessages, reactions: FakeReactions | None = None
    ) -> None:
        self.log: list[str] = []
        self.messages = messages
        self.reactions = reactions or FakeReactions()
        self.counters = FakeCounters()
        self.events = FakeChannelEvents()
        self.bus = FakeBus(self.log)
        self.service = ReactionService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(MessageRepository, self.messages),
            cast(ReactionRepository, self.reactions),
            cast(ChannelCounterRepository, self.counters),
            cast(ChannelEventRepository, self.events),
            AccessService(cast(CoreClient, core)),
            cast(EventBus, self.bus),
        )


class PinFakes:
    """A PinService on fakes. `log` shows the order of commit, rollback and publish."""

    def __init__(
        self, core: FakeCore, messages: FakeMessages, pins: list[Pin] | None = None
    ) -> None:
        self.log: list[str] = []
        self.messages = messages
        self.pins = FakePins(messages, pins)
        self.reactions = FakeReactions()
        self.counters = FakeCounters()
        self.events = FakeChannelEvents()
        self.bus = FakeBus(self.log)
        self.service = PinService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(MessageRepository, self.messages),
            cast(PinRepository, self.pins),
            cast(ReactionRepository, self.reactions),
            cast(ChannelCounterRepository, self.counters),
            cast(ChannelEventRepository, self.events),
            AccessService(cast(CoreClient, core)),
            cast(EventBus, self.bus),
        )


class SyncFakes:
    """A SyncService on fakes. `log` shows commit and rollback."""

    def __init__(
        self,
        core: FakeCore,
        stored: list[ChannelEvent] | None = None,
        last_seqs: dict[uuid.UUID, int] | None = None,
    ) -> None:
        self.log: list[str] = []
        self.events = FakeChannelEvents(stored)
        self.counters = FakeCounters(per_channel=last_seqs)
        self.service = SyncService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(ChannelEventRepository, self.events),
            cast(ChannelCounterRepository, self.counters),
            AccessService(cast(CoreClient, core)),
        )


class FakePresence:
    """
    Stands in for PresenceStore. `lines` are the open sockets per user, `heartbeats` the refreshed
    ones, `sent` the online and offline calls and `online` what list_online finds per team.
    """

    def __init__(self) -> None:
        self.lines: dict[uuid.UUID, set[str]] = {}
        self.heartbeats: list[tuple[uuid.UUID, str]] = []
        self.sent: list[tuple[str, uuid.UUID, list[uuid.UUID]]] = []
        self.online: dict[uuid.UUID, list[uuid.UUID]] = {}

    async def socket_opened(self, user_id: uuid.UUID, socket_id: str) -> bool:
        first = not self.lines.get(user_id)
        self.lines.setdefault(user_id, set()).add(socket_id)
        return first

    async def heartbeat(self, user_id: uuid.UUID, socket_id: str) -> None:
        self.heartbeats.append((user_id, socket_id))

    async def socket_closed(self, user_id: uuid.UUID, socket_id: str) -> bool:
        self.lines.get(user_id, set()).discard(socket_id)
        return not self.lines.get(user_id)

    async def go_online(self, user_id: uuid.UUID, team_ids: Iterable[uuid.UUID]) -> None:
        self.sent.append(("online", user_id, list(team_ids)))

    async def go_offline(self, user_id: uuid.UUID, team_ids: Iterable[uuid.UUID]) -> None:
        self.sent.append(("offline", user_id, list(team_ids)))

    async def list_online(self, team_id: uuid.UUID) -> list[uuid.UUID]:
        return self.online.get(team_id, [])


class ReadStateFakes:
    """A ReadStateService on fakes. `log` shows the order of commit, rollback and send_to_user."""

    def __init__(
        self,
        core: FakeCore,
        last_seq: int = 0,
        counts: dict[uuid.UUID, tuple[int, int]] | None = None,
    ) -> None:
        self.log: list[str] = []
        self.read_state = FakeReadState(counts)
        self.counters = FakeCounters(last_seq)
        self.bus = FakeBus(self.log)
        self.service = ReadStateService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(ReadStateRepository, self.read_state),
            cast(ChannelCounterRepository, self.counters),
            AccessService(cast(CoreClient, core)),
            cast(EventBus, self.bus),
        )


class FakeSocket:
    """Stands in for a WebSocket in the Gateway: records frames and the close, or raises `error`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed: tuple[int, str | None] | None = None
        self._error = error

    async def send_json(self, data: Any) -> None:
        if self._error:
            raise self._error
        self.frames.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)
