import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelEvent
from app.schemas.events import EventType

EMPTY_TEXT = literal("", JSONB)
NO_MENTIONS = literal([], JSONB)


class ChannelEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_channel_event(
        self, channel_id: uuid.UUID, seq: int, event_type: str, payload: dict[str, Any]
    ) -> None:
        """One row of the event history, also the outbox for the relay (published_at is null)."""
        self._session.add(
            ChannelEvent(channel_id=channel_id, seq=seq, type=event_type, payload=payload)
        )
        await self._session.flush()

    async def list_events_after(
        self, channel_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[ChannelEvent]:
        """Oldest first: the events with seq > after_seq"""
        stmt = (
            select(ChannelEvent)
            .where(ChannelEvent.channel_id == channel_id, ChannelEvent.seq > after_seq)
            .order_by(ChannelEvent.seq)
            .limit(limit)
        )
        return list(await self._session.scalars(stmt))

    async def clear_message_text(
        self, channel_id: uuid.UUID, seq: int, message_id: uuid.UUID, was_edited: bool
    ) -> None:
        """A deleted message's text also leaves its earlier events. `seq` is the message's own."""
        created = (
            update(ChannelEvent)
            .where(
                ChannelEvent.channel_id == channel_id,
                ChannelEvent.seq == seq,  # message.created has the message's own seq
                ChannelEvent.type == EventType.MESSAGE_CREATED,
            )
            .values(
                payload=func.jsonb_set(
                    func.jsonb_set(ChannelEvent.payload, ["message", "body"], EMPTY_TEXT),
                    ["message", "mentions"],
                    NO_MENTIONS,
                )
            )
        )
        edited = (
            update(ChannelEvent)
            .where(
                ChannelEvent.channel_id == channel_id,
                # edits come after the message, so the (channel_id, seq) index narrows the scan
                ChannelEvent.seq > seq,
                ChannelEvent.type == EventType.MESSAGE_EDITED,
                ChannelEvent.payload["message_id"].astext == str(message_id),
            )
            .values(
                payload=func.jsonb_set(
                    func.jsonb_set(ChannelEvent.payload, ["body"], EMPTY_TEXT),
                    ["mentions"],
                    NO_MENTIONS,
                )
            )
        )
        await self._session.execute(created)
        # shortcut: for an edited message this scans every later event of the channel while
        # the delete holds the channel's counter lock; index payload->>'message_id' if CHAT-224
        # shows it.
        if was_edited:
            await self._session.execute(edited)

    async def delete_events_before(self, cutoff: datetime) -> int:
        """Old published events only, oldest first per channel, so sync sees what was removed."""
        old = (
            select(ChannelEvent.channel_id, func.max(ChannelEvent.seq).label("last_old_seq"))
            # the relay publishes in id order, so a channel's published events come before the rest
            .where(ChannelEvent.created_at < cutoff, ChannelEvent.published_at.is_not(None))
            .group_by(ChannelEvent.channel_id)
            .subquery()
        )
        stmt = delete(ChannelEvent).where(
            ChannelEvent.channel_id == old.c.channel_id,
            ChannelEvent.seq <= old.c.last_old_seq,
        )
        return (await self._session.execute(stmt)).rowcount  # type: ignore[attr-defined]

    async def lock_unpublished(self, limit: int) -> list[ChannelEvent]:
        """Oldest first. No SKIP LOCKED: a row a delete holds must not be passed over."""
        stmt = (
            select(ChannelEvent)
            .where(ChannelEvent.published_at.is_(None))
            .order_by(ChannelEvent.id)
            .limit(limit)
            .with_for_update()
        )
        return list(await self._session.scalars(stmt))

    async def mark_published(self, ids: list[int]) -> None:
        stmt = update(ChannelEvent).where(ChannelEvent.id.in_(ids)).values(published_at=func.now())
        await self._session.execute(stmt)
