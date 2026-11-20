"""MessageRepository.is_deleted against the real Postgres: the push worker's check."""

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Message
from app.repositories import MessageRepository

pytestmark = pytest.mark.integration

CHANNEL_ID = uuid.uuid4()


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    async with sessions.begin() as session:
        await session.execute(delete(Message).where(Message.channel_id == CHANNEL_ID))
    await engine.dispose()


async def new_message(sessions) -> Message:
    async with sessions.begin() as session:
        return await MessageRepository(session).insert_message(
            channel_id=CHANNEL_ID,
            author_id=uuid.uuid4(),
            seq=1,
            client_msg_id=uuid.uuid4(),
            body="secret",
            mentions=[],
        )


async def test_a_new_message_is_not_deleted(sessions):
    message = await new_message(sessions)

    async with sessions.begin() as session:
        assert await MessageRepository(session).is_deleted(message.id) is False


async def test_a_soft_deleted_message_is_deleted(sessions):
    message = await new_message(sessions)
    async with sessions.begin() as session:
        await MessageRepository(session).soft_delete_message(message.id)

    async with sessions.begin() as session:
        assert await MessageRepository(session).is_deleted(message.id) is True


async def test_a_deleted_message_is_seen_by_a_session_that_loaded_it_before(sessions):
    """The worker keeps one session: a copy it loaded earlier must not hide the delete."""
    message = await new_message(sessions)
    async with sessions() as worker:
        async with worker.begin():
            await worker.get(Message, message.id)  # now in the worker session's memory
        async with sessions.begin() as api:
            await MessageRepository(api).soft_delete_message(message.id)

        async with worker.begin():
            assert await MessageRepository(worker).is_deleted(message.id) is True


async def test_an_unknown_message_counts_as_deleted(sessions):
    async with sessions.begin() as session:
        assert await MessageRepository(session).is_deleted(uuid.uuid4()) is True
