from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base

OUTBOX_DB = "chat_outbox_test"


@pytest.fixture
async def outbox_db() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """
    An empty database of its own, for tests that read the whole outbox: the shared one may hold
    other tests' events, and a running relay would publish them first.
    """
    base = make_url(get_settings().DATABASE_URL)
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{OUTBOX_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{OUTBOX_DB}"'))
    engine = create_async_engine(base.set(database=OUTBOX_DB))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{OUTBOX_DB}" WITH (FORCE)'))
        await admin.dispose()
