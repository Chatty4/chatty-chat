import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import Conflict, NotFound, PermissionDenied
from app.models import ChannelEvent as ChannelEventRow
from app.repositories import ChannelCounterRepository, ChannelEventRepository
from app.schemas.core import Memberships
from app.schemas.events import ChannelEvent
from app.schemas.sync import (
    ChannelEventsResponse,
    ChannelSyncError,
    ChannelSyncResult,
    SyncResponse,
)
from app.services.access_service import AccessService

MAX_SYNC_EVENTS = 500  # per channel (api-chat.md, POST /sync)
MAX_PARALLEL_CHECKS = 20  # chatty-core calls at once per /sync; httpx's pool has 100


def _cleaned_up(rows: list[ChannelEventRow], after_seq: int, last_seq: int) -> bool:
    """Every event takes the next seq of its channel, so a missing first one was cleaned up."""
    return after_seq < last_seq and (not rows or rows[0].seq != after_seq + 1)


def _to_events(rows: list[ChannelEventRow]) -> list[ChannelEvent]:
    return [ChannelEvent.model_validate(row, from_attributes=True) for row in rows]


class SyncService:
    def __init__(
        self,
        session: AsyncSession,
        events: ChannelEventRepository,
        counters: ChannelCounterRepository,
        access: AccessService,
    ) -> None:
        self._session = session
        self._events = events
        self._counters = counters
        self._access = access

    async def sync(self, user: Memberships, cursors: dict[uuid.UUID, int]) -> SyncResponse:
        """The missed events of each channel. A channel the user can't read gets its own error."""
        channels: dict[uuid.UUID, ChannelSyncResult | ChannelSyncError] = {}
        readable: dict[uuid.UUID, int] = {}
        # At the same time: each unknown channel is a chatty-core call, and 404s aren't cached.
        # They use only the core client, never the session. The semaphore keeps one request
        # with 500 unknown channels from sending 500 calls at once.
        limit = asyncio.Semaphore(MAX_PARALLEL_CHECKS)

        async def check_can_read(channel_id: uuid.UUID) -> None:
            async with limit:
                await self._access.check_can_read(user, channel_id)

        checks = await asyncio.gather(
            *(check_can_read(channel_id) for channel_id in cursors), return_exceptions=True
        )
        for (channel_id, after_seq), check in zip(cursors.items(), checks, strict=True):
            if isinstance(check, NotFound | PermissionDenied):
                channels[channel_id] = ChannelSyncError(error=str(check.code))
            elif isinstance(check, BaseException):
                raise check  # core_unavailable and the like fail the whole request, as before
            else:
                readable[channel_id] = after_seq

        # shortcut: up to two queries per channel (500 channels max);
        # batch them if CHAT-224 shows it.
        async with self._session.begin():
            for channel_id, after_seq in readable.items():
                channels[channel_id] = await self._sync_channel(channel_id, after_seq)
        return SyncResponse(channels=channels)

    async def _sync_channel(self, channel_id: uuid.UUID, after_seq: int) -> ChannelSyncResult:
        last_seq = await self._counters.get_last_seq(channel_id)  # first, as in list_history
        if last_seq - after_seq > MAX_SYNC_EVENTS:
            return ChannelSyncResult(events=[], last_seq=last_seq, gap_too_large=True)

        rows = await self._events.list_events_after(channel_id, after_seq, MAX_SYNC_EVENTS)
        if _cleaned_up(rows, after_seq, last_seq):
            return ChannelSyncResult(events=[], last_seq=last_seq, gap_too_large=True)
        return ChannelSyncResult(events=_to_events(rows), last_seq=last_seq, gap_too_large=False)

    async def list_channel_events(
        self, user: Memberships, channel_id: uuid.UUID, after_seq: int, limit: int
    ) -> ChannelEventsResponse:
        """One channel's events after after_seq, oldest first. 409 if they were cleaned up."""
        await self._access.check_can_read(user, channel_id)

        async with self._session.begin():
            last_seq = await self._counters.get_last_seq(channel_id)
            # One extra row tells whether there is a next page.
            rows = await self._events.list_events_after(channel_id, after_seq, limit + 1)

        if _cleaned_up(rows, after_seq, last_seq):
            raise Conflict(
                ErrorCode.GAP_TOO_LARGE, "The events were cleaned up; reload the history"
            )
        return ChannelEventsResponse(
            events=_to_events(rows[:limit]), last_seq=last_seq, has_more=len(rows) > limit
        )
