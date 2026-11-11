import random

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.jobs.relay import asyncpg_connection, take_relay_lock

pytestmark = pytest.mark.integration


async def test_only_one_connection_holds_the_relay_lock():
    # its own lock id, so a relay running against the same database doesn't matter
    lock_id = random.randint(1_000_000, 9_000_000)
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        async with engine.connect() as first, engine.connect() as second:
            a, b = await asyncpg_connection(first), await asyncpg_connection(second)

            assert await take_relay_lock(a, lock_id) is True
            assert await take_relay_lock(b, lock_id) is False

            await a.fetchval("SELECT pg_advisory_unlock($1)", lock_id)
            assert await take_relay_lock(b, lock_id) is True
            await b.fetchval("SELECT pg_advisory_unlock($1)", lock_id)
    finally:
        await engine.dispose()
