import asyncio
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    app.state.core_http = httpx.AsyncClient(
        base_url=settings.CORE_INTERNAL_URL,
        headers={"X-Service-Token": settings.CORE_SERVICE_TOKEN.get_secret_value()},
        timeout=2.0,
    )
    app.state.connections = ConnectionRegistry()
    events_redis = Redis.from_url(
        settings.REDIS_URL, decode_responses=True, retry=Retry(NoBackoff(), 0)
    )
    events_task = asyncio.create_task(listen_core_events(events_redis))
    try:
        yield
    finally:
        events_task.cancel()
        with suppress(asyncio.CancelledError):
            await events_task
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
