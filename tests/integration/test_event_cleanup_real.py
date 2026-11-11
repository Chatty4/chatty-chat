import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import ChannelEvent
from app.schemas.events import EventType
from app.services.event_cleanup import clean_up_events

pytestmark = pytest.mark.integration

# The real now, so the cleanup deletes nothing in the shared database that the nightly job wouldn't.
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=31)
NEW = NOW - timedelta(days=1)


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


async def add_events(
    sessions, channel_id: uuid.UUID, *created_at: datetime, unpublished_from: int | None = None
) -> None:
    """One event per time, with seq 1, 2, 3... in that order. Published up to unpublished_from."""
    async with sessions.begin() as session:
        session.add_all(
            ChannelEvent(
                channel_id=channel_id,
                seq=seq,
                type=EventType.MESSAGE_CREATED,
                payload={},
                created_at=time,
                published_at=None if unpublished_from and seq >= unpublished_from else time,
            )
            for seq, time in enumerate(created_at, start=1)
        )


async def seqs_left(sessions, channel_id: uuid.UUID) -> list[int]:
    async with sessions() as session:
        stmt = (
            select(ChannelEvent.seq)
            .where(ChannelEvent.channel_id == channel_id)
            .order_by(ChannelEvent.seq)
        )
        return list(await session.scalars(stmt))


async def clean_up(sessions) -> None:
    async with sessions() as session:
        await clean_up_events(session, NOW)


async def test_deletes_events_older_than_30_days_and_keeps_newer(sessions, channels):
    room = channels()
    await add_events(sessions, room, OLD, OLD, NEW)

    await clean_up(sessions)

    assert await seqs_left(sessions, room) == [3]


async def test_never_leaves_a_hole_in_the_middle(sessions, channels):
    # seq 2 committed with a newer created_at than seq 3 (it waited for the counter lock).
    # Deleting only by created_at would keep seq 2 and let sync miss seq 1 and 3 unnoticed.
    room = channels()
    await add_events(sessions, room, OLD, NEW, OLD, NEW)

    await clean_up(sessions)

    assert await seqs_left(sessions, room) == [4]


async def test_each_channel_is_cleaned_on_its_own(sessions, channels):
    busy, quiet = channels(), channels()
    await add_events(sessions, busy, OLD, NEW)
    await add_events(sessions, quiet, NEW)

    await clean_up(sessions)

    assert await seqs_left(sessions, busy) == [2]
    assert await seqs_left(sessions, quiet) == [1]


async def test_keeps_old_events_the_relay_has_not_published(sessions, channels):
    room = channels()
    await add_events(sessions, room, OLD, OLD, unpublished_from=1)

    await clean_up(sessions)

    assert await seqs_left(sessions, room) == [1, 2]


async def test_deletes_only_up_to_the_first_unpublished_event(sessions, channels):
    room = channels()
    await add_events(sessions, room, OLD, OLD, NEW, unpublished_from=2)

    await clean_up(sessions)

    assert await seqs_left(sessions, room) == [2, 3]


async def test_keeps_everything_when_nothing_is_old(sessions, channels):
    room = channels()
    await add_events(sessions, room, NEW, NEW)

    await clean_up(sessions)

    assert await seqs_left(sessions, room) == [1, 2]
