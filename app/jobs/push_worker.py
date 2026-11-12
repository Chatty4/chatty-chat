import asyncio
import logging

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.core import CoreClient
from app.clients.kafka import create_consumer
from app.clients.presence import PresenceStore
from app.clients.web_push import PushDedupe, WebPushClient
from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailable
from app.core.logging import setup_logging
from app.repositories import PushSubscriptionRepository
from app.schemas.events import ChannelEvent
from app.services.push_service import PushService

logger = logging.getLogger("app.jobs.push_worker")

GROUP_ID = "push-worker"  # shared: each event is handled by one worker (events.md)
RETRY_SECONDS = 1


async def handle_until_done(service: PushService, event: ChannelEvent) -> None:
    """Retries until it works: committing after a failure would lose the event."""
    while True:
        try:
            await service.handle(event)
            return
        except ServiceUnavailable, SQLAlchemyError, RedisError:
            logger.exception("Push failed for seq %s, retrying", event.seq)
            await asyncio.sleep(RETRY_SECONDS)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.SQL_ECHO)
    private_key = settings.VAPID_PRIVATE_KEY.get_secret_value()
    if not private_key:
        raise SystemExit("VAPID_PRIVATE_KEY is not set, see .env.example")

    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    redis = Redis.from_url(
        settings.REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
    )
    core_http = httpx.AsyncClient(
        base_url=settings.CORE_INTERNAL_URL,
        headers={"X-Service-Token": settings.CORE_SERVICE_TOKEN.get_secret_value()},
        timeout=2.0,
    )
    consumer = create_consumer(GROUP_ID)

    try:
        # expire_on_commit=False: PushService reads the rows after their transaction
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            service = PushService(
                session,
                PushSubscriptionRepository(session),
                CoreClient(core_http, redis),
                PresenceStore(redis),
                PushDedupe(redis),
                WebPushClient(private_key, settings.VAPID_SUBJECT),
            )
            await consumer.start()
            logger.info("Push worker started")
            # shortcut: one event at a time; run more workers
            # (up to 6, one per partition) if it lags
            async for record in consumer:
                if record.value is not None:  # None: a bad record, skip it
                    await handle_until_done(service, record.value)
                await consumer.commit()
    finally:
        await consumer.stop()
        await core_http.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
