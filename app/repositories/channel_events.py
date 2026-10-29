import uuid
from typing import Any

from sqlalchemy import func, literal, select, update
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
        self, channel_id: uuid.UUID, seq: int, message_id: uuid.UUID
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
        await self._session.execute(edited)
