import asyncio
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.core.config import get_settings
from app.models import ChannelEvent, Message

pytestmark = pytest.mark.integration

TEST_DB = "chat_migrations_test"


async def alembic(cmd, revision: str) -> None:
    # No ini file, so env.py skips fileConfig and leaves the app's logging alone.
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    # env.py calls asyncio.run, which can't run inside pytest's event loop.
    await asyncio.to_thread(cmd, cfg, revision)


@pytest.fixture
async def empty_db(monkeypatch):
    base = make_url(get_settings().DATABASE_URL)
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    url = base.set(database=TEST_DB).render_as_string(hide_password=False)
    # env.py reads DATABASE_URL from the cached settings
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()
    get_settings.cache_clear()
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
    await admin.dispose()


def message(**overrides):
    row = {
        "id": uuid.uuid4(),
        "channel_id": uuid.uuid4(),
        "seq": 1,
        "author_id": uuid.uuid4(),
        "client_msg_id": uuid.uuid4(),
        "body": "hi",
    }
    return row | overrides


async def insert_fails(engine, model, rows, constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with engine.begin() as conn:
            await conn.execute(insert(model), rows)


async def test_migrations_on_empty_db(empty_db):
    await alembic(command.upgrade, "head")

    channel_id, author_id, client_msg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await insert_fails(
        empty_db,
        Message,
        [message(channel_id=channel_id), message(channel_id=channel_id)],
        "uq_messages_channel_id",
    )
    await insert_fails(
        empty_db,
        Message,
        [
            message(author_id=author_id, client_msg_id=client_msg_id),
            message(author_id=author_id, client_msg_id=client_msg_id, seq=2),
        ],
        "uq_messages_author_id",
    )
    event = {"channel_id": channel_id, "seq": 1, "type": "message.created", "payload": {}}
    await insert_fails(empty_db, ChannelEvent, [event, event], "uq_channel_events_channel_id")

    await alembic(command.downgrade, "base")
