"""Labels attachments for search, one at a time (CHAT-299).
Run: python -m app.jobs.label_attachments"""

import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.jobs.relay import asyncpg_connection, take_relay_lock
from app.labeling.label import load_models
from app.repositories import AttachmentRepository
from app.services.attachment_labeling import label_next

logger = logging.getLogger("app.jobs.label_attachments")

IDLE_SECONDS = 30  # new uploads wait at most this long once the queue is empty
RETRY_SECONDS = 5
LOCK_RETRY_SECONDS = 30
LABELER_LOCK_ID = 299  # Postgres advisory lock: two labelers could claim the same file


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.SQL_ECHO)
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # warns on every slightly broken PDF
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)  # warns on every image without text
    engine = create_async_engine(settings.DATABASE_URL)

    try:
        # control: holds the labeler lock until the process ends
        async with engine.connect() as control, async_sessionmaker(engine)() as session:
            while not await take_relay_lock(await asyncpg_connection(control), LABELER_LOCK_ID):
                logger.info("Another labeler is running, waiting for its lock")
                await asyncio.sleep(LOCK_RETRY_SECONDS)

            await asyncio.to_thread(load_models)
            attachments = AttachmentRepository(session)
            logger.info("Labeler started")
            while True:
                try:
                    found = await label_next(session, attachments)
                except SQLAlchemyError:
                    logger.exception("Labeling failed on the database, retrying")
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                if not found:
                    await asyncio.sleep(IDLE_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
