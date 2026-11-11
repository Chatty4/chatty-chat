import asyncio
import logging
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


async def asyncpg_connection(conn: AsyncConnection) -> asyncpg.Connection:
    """The asyncpg connection under an open SQLAlchemy one; outside any transaction."""
    return cast(asyncpg.Connection, (await conn.get_raw_connection()).driver_connection)


async def take_relay_lock(conn: asyncpg.Connection, lock_id: int = RELAY_LOCK_ID) -> bool:
    """Held until the connection closes."""
    return bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id))


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.SQL_ECHO)
    engine = create_async_engine(settings.DATABASE_URL)
    producer = create_producer()

    try:
        # control: holds the relay lock for as long as the relay runs
        async with engine.connect() as control, async_sessionmaker(engine)() as session:
            lock_conn = await asyncpg_connection(control)
            while not await take_relay_lock(lock_conn):
                logger.info("Another relay is running, waiting for its lock")
                await asyncio.sleep(LOCK_RETRY_SECONDS)
                
            await producer.start()
            events = ChannelEventRepository(session)
            logger.info("Relay started")
            while True:
                try:
                    sent = await publish_batch(session, events, producer, settings.KAFKA_TOPIC)
                except KafkaError, SQLAlchemyError:
                    logger.exception("Relay batch failed, retrying; the events stay unpublished")
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                if sent < BATCH_SIZE:
                    await asyncio.sleep(POLL_SECONDS)
    finally:
        await producer.stop()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
