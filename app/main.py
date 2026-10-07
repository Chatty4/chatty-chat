from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.router import api_router
from app.api.routers import health
from app.core.config import get_settings
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="chatty-chat", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(api_router, prefix="/api/chat/v1")
    return app


app = create_app()
