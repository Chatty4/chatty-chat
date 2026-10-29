import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.error_handlers import register_exception_handlers
from app.api.router import api_router
from app.api.routers import health
from app.clients.core_events import listen_core_events
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.connection_registry import ConnectionRegistry
from app.services.gateway import Gateway

logger = logging.getLogger(__name__)


def log_crash(task: asyncio.Task) -> None:
    """Without it, a crashed background task stops silently and /health still says ok."""
    if not task.cancelled() and task.exception() is not None:
        logger.critical("%s stopped", task.get_name(), exc_info=task.exception())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = Redis.from_url(
        settings.REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
    )
    app.state.core_http = httpx.AsyncClient(
        base_url=settings.CORE_INTERNAL_URL,
        headers={"X-Service-Token": settings.CORE_SERVICE_TOKEN.get_secret_value()},
        timeout=2.0,
    )
    app.state.connections = ConnectionRegistry()
    # pubsub.listen() still blocks without a timeout; these apply to the other commands.
    # redis-py 8.1 turns on TCP keepalive by default (30s idle, 5s, 3 tries), so a silently
    # dorpped connection fails in about 45s and the retry loops reconnect.
    events_redis = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        retry=Retry(NoBackoff(), 0),
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    app.state.gateway = Gateway(events_redis, app.state.connections)
    gateway_task = asyncio.create_task(app.state.gateway.run(), name="gateway")
    events_task = asyncio.create_task(listen_core_events(events_redis), name="core events")
    gateway_task.add_done_callback(log_crash)
    events_task.add_done_callback(log_crash)
    try:
        yield
    finally:
        events_task.cancel()
        gateway_task.cancel()
        with suppress(asyncio.CancelledError):
            await events_task
        with suppress(asyncio.CancelledError):
            await gateway_task
        await events_redis.aclose()
        await app.state.core_http.aclose()
        await app.state.redis.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.SQL_ECHO)
    app = FastAPI(title="chatty-chat", version="0.1.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router, prefix="/api/chat/v1")
    return app


app = create_app()
