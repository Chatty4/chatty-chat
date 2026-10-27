import uuid
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.core import CoreClient
from app.models import Message
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreChannel, Memberships, TeamMembership
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import MessageService


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
    `stored` are the messages get_message finds. With `deleted_meanwhile`, update_message_body and
    soft_delete_message find nothing to change, as if the message was deleted after the service
    checked it.
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


class FakeCounters:
    def __init__(self, last_seq: int = 0) -> None:
        self.last_seq = last_seq

    async def next_seq(self, channel_id: uuid.UUID) -> int:
        self.last_seq += 1
        return self.last_seq


class FakeChannelEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[uuid.UUID, int, str, dict[str, Any]]] = []

    async def insert_channel_event(
        self, channel_id: uuid.UUID, seq: int, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.rows.append((channel_id, seq, event_type, payload))


class FakeReadState:
    def __init__(self) -> None:
        self.positions: dict[tuple[uuid.UUID, uuid.UUID], int] = {}

    async def advance_read_position(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, seq: int
    ) -> None:
        key = (user_id, channel_id)
        self.positions[key] = max(self.positions.get(key, 0), seq)


class FakeBus:
    """Records what was published, and writes "publish" to `log` to show when."""

    def __init__(self, log: list[str] | None = None) -> None:
        self._log = log if log is not None else []
        self.published: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def publish(self, channel_id: uuid.UUID, event: dict[str, Any]) -> None:
        self._log.append("publish")
        self.published.append((channel_id, event))


class MessageFakes:
    """A MessageService on fakes. `log` shows the order of commit, rollback and publish."""

    def __init__(self, core: FakeCore, messages: FakeMessages | None = None) -> None:
        self.log: list[str] = []
        self.messages = messages or FakeMessages()
        self.counters = FakeCounters()
        self.events = FakeChannelEvents()
        self.read_state = FakeReadState()
        self.bus = FakeBus(self.log)
        # The fakes only duck-type the real classes, so cast them for the type checker.
        self.service = MessageService(
            cast(AsyncSession, FakeSession(self.log)),
            cast(MessageRepository, self.messages),
            cast(ChannelCounterRepository, self.counters),
            cast(ChannelEventRepository, self.events),
            cast(ReadStateRepository, self.read_state),
            AccessService(cast(CoreClient, core)),
            cast(EventBus, self.bus),
        )
