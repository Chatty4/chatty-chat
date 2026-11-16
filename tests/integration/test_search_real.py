import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.core.config import get_settings
from app.models import Message
from app.repositories import MessageRepository, PinRepository, ReactionRepository
from app.schemas.core import TeamMembership, TeamRole
from app.schemas.search import SearchResponse
from app.services.access_service import AccessService
from app.services.search_service import SearchService
from tests.fakes import FakeCore, memberships

pytestmark = pytest.mark.integration

USER_ID, OTHER_ID, TEAM = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NOW = datetime.now(UTC)
seqs = itertools.count(1)


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def rooms(sessions):
    """A public channel of the team, and a private one the user is not in."""
    public, private = uuid.uuid4(), uuid.uuid4()
    yield public, private
    async with sessions.begin() as session:
        await session.execute(delete(Message).where(Message.channel_id.in_([public, private])))


async def add(
    sessions,
    channel_id: uuid.UUID,
    body: str,
    *,
    author_id: uuid.UUID = USER_ID,
    created_at: datetime = NOW,
    deleted: bool = False,
) -> uuid.UUID:
    message = Message(
        channel_id=channel_id,
        author_id=author_id,
        seq=next(seqs),
        client_msg_id=uuid.uuid4(),
        body=body,
        created_at=created_at,
        deleted_at=NOW if deleted else None,
    )
    async with sessions.begin() as session:
        session.add(message)
    return message.id


async def search(
    sessions,
    public: uuid.UUID,
    q: str,
    *,
    author_id: uuid.UUID | None = None,
    after: datetime | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> SearchResponse:
    team = TeamMembership(team_id=TEAM, role=TeamRole.MEMBER, public_channel_ids=[public])
    user = memberships(USER_ID, teams=[team])
    async with sessions() as session:
        service = SearchService(
            MessageRepository(session),
            ReactionRepository(session),
            PinRepository(session),
            AccessService(cast(CoreClient, FakeCore())),
        )
        return await service.search(user, TEAM, q, None, author_id, None, after, cursor, limit)


def bodies(page: SearchResponse) -> list[str]:
    return [result.message.body for result in page.results]


async def test_finds_other_forms_of_the_word_and_highlights_them(sessions, rooms):
    public, _ = rooms
    await add(sessions, public, "deploying the app now")
    await add(sessions, public, "lunch at noon")

    page = await search(sessions, public, "deploy")

    assert bodies(page) == ["deploying the app now"]
    assert "<mark>deploying</mark>" in page.results[0].highlight


async def test_never_searches_channels_the_user_cant_read(sessions, rooms):
    public, private = rooms
    await add(sessions, private, "deploy secret")

    page = await search(sessions, public, "deploy")

    assert page.results == []


async def test_never_returns_deleted_messages(sessions, rooms):
    public, _ = rooms
    await add(sessions, public, "deploy was deleted", deleted=True)

    page = await search(sessions, public, "deploy")

    assert page.results == []


async def test_mentions_are_not_searchable(sessions, rooms):
    public, _ = rooms
    await add(sessions, public, "hi <@71dbb4fd-b3df-4cf2-8442-a04a5875998c>")

    page = await search(sessions, public, "8442")

    assert page.results == []


async def test_highlight_escapes_the_message_text(sessions, rooms):
    public, _ = rooms
    await add(sessions, public, "deploy if 1 < 2 & ok")

    page = await search(sessions, public, "deploy")

    assert page.results[0].highlight == "<mark>deploy</mark> if 1 &lt; 2 &amp; ok"


async def test_filters_by_author_and_time(sessions, rooms):
    public, _ = rooms
    await add(sessions, public, "deploy by other", author_id=OTHER_ID)
    await add(sessions, public, "deploy last month", created_at=NOW - timedelta(days=30))
    await add(sessions, public, "deploy today")

    page = await search(
        sessions, public, "deploy", author_id=USER_ID, after=NOW - timedelta(days=1)
    )

    assert bodies(page) == ["deploy today"]


async def test_pages_through_equal_matches_newest_first_without_repeats(sessions, rooms):
    public, _ = rooms
    for days_ago in (3, 2, 1):
        await add(sessions, public, f"deploy {days_ago}", created_at=NOW - timedelta(days=days_ago))

    first = await search(sessions, public, "deploy", limit=2)
    second = await search(sessions, public, "deploy", cursor=first.next_cursor, limit=2)

    assert bodies(first) == ["deploy 1", "deploy 2"]
    assert bodies(second) == ["deploy 3"]
    assert second.next_cursor is None
