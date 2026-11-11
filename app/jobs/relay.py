import asyncio
import logging
from contextlib import suppress
from typing import cast

import asyncpg
from aiokafka.errors import KafkaError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine

from app.clients.kafka import create_producer
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.repositories import ChannelEventRepository
from app.services.outbox_relay import BATCH_SIZE, publish_batch

logger = logging.getLogger("app.jobs.relay")

POLL_SECONDS = 0.2
RETRY_SECONDS = 1
LOCK_RETRY_SECONDS = 5
RELAY_LOCK_ID = 189  # Postgres advisory lock: a second relay could reorder a channel's events
NOTIFY_CHANNEL = "chat_events"  # sent by the channel_events trigger


async def asyncpg_connection(conn: AsyncConnection) -> asyncpg.Connection:
    """The asyncpg connection under an open SQLAlchemy one; outside any transaction."""
    return cast(asyncpg.Connection, (await conn.get_raw_connection()).driver_connection)


async def take_relay_lock(conn: asyncpg.Connection, lock_id: int = RELAY_LOCK_ID) -> bool:
    """Held until the connection closes."""
    return bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id))


async def wait_for(wake: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await wake.wait()


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.SQL_ECHO)
    engine = create_async_engine(settings.DATABASE_URL)
    producer = create_producer()

    try:
        # control: holds the relay lock and LISTENs for new events
        async with engine.connect() as control, async_sessionmaker(engine)() as session:
            lock_conn = await asyncpg_connection(control)
            while not await take_relay_lock(lock_conn):
                logger.info("Another relay is running, waiting for its lock")
                await asyncio.sleep(LOCK_RETRY_SECONDS)

            await producer.start()
            wake = asyncio.Event()
            await lock_conn.add_listener(NOTIFY_CHANNEL, lambda *_: wake.set())
            events = ChannelEventRepository(session)
            logger.info("Relay started")
            while True:
                wake.clear()  # before the batch, so a NOTIFY during it isn't lost
                try:
                    sent = await publish_batch(session, events, producer, settings.KAFKA_TOPIC)
                except KafkaError, SQLAlchemyError:
                    logger.exception("Relay batch failed, retrying; the events stay unpublished")
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                if sent < BATCH_SIZE:
                    # the poll still covers a NOTIFY lost while the connection was down
                    await wait_for(wake, POLL_SECONDS)
    finally:
        await producer.stop()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
