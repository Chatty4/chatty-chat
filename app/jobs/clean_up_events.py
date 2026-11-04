import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.event_cleanup import clean_up_events

logger = logging.getLogger("app.jobs.clean_up_events")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.SQL_ECHO)
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            deleted = await clean_up_events(session, datetime.now(UTC))
        logger.info("Deleted %s channel events older than 30 days", deleted)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
