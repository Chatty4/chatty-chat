import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import ChannelEvent
from app.repositories import ChannelEventRepository
from app.schemas.events import EventType

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def channels(sessions):
    """Fresh channel ids, and their events deleted afterwards."""
    ids: list[uuid.UUID] = []

    def new() -> uuid.UUID:
        ids.append(uuid.uuid4())
        return ids[-1]

    yield new
    async with sessions.begin() as session:
        await session.execute(delete(ChannelEvent).where(ChannelEvent.channel_id.in_(ids)))


async def add_events(sessions, channel_id: uuid.UUID, count: int) -> list[int]:
    """`count` unpublished events with seq 1, 2, 3...; returns their ids."""
    rows = [
        ChannelEvent(channel_id=channel_id, seq=seq, type=EventType.MESSAGE_CREATED, payload={})
        for seq in range(1, count + 1)
    ]
    async with sessions.begin() as session:
        session.add_all(rows)
    return [row.id for row in rows]


async def unpublished(sessions, *channel_ids: uuid.UUID) -> list[tuple[uuid.UUID, int]]:
    """What lock_unpublished returns for these channels (the shared database may hold others)."""
    async with sessions.begin() as session:
        rows = await ChannelEventRepository(session).lock_unpublished(limit=10_000)
    return [(row.channel_id, row.seq) for row in rows if row.channel_id in channel_ids]


async def test_unpublished_events_come_oldest_first(sessions, channels):
    first, second = channels(), channels()
    await add_events(sessions, first, 2)
    await add_events(sessions, second, 1)

    assert await unpublished(sessions, first, second) == [(first, 1), (first, 2), (second, 1)]


async def test_published_events_are_left_out_and_marked_with_a_time(sessions, channels):
    room = channels()
    sent, waiting = await add_events(sessions, room, 2)

    async with sessions.begin() as session:
        await ChannelEventRepository(session).mark_published([sent])

    assert await unpublished(sessions, room) == [(room, 2)]
    async with sessions() as session:
        stmt = select(ChannelEvent.id, ChannelEvent.published_at).where(
            ChannelEvent.id.in_([sent, waiting])
        )
        published = dict((await session.execute(stmt)).all())
    assert published[sent] is not None
    assert published[waiting] is None
