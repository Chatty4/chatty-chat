import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.schemas.health import CheckStatus, HealthStatus
from app.services.health_service import HealthService

pytestmark = pytest.mark.integration


async def test_health_against_real_db_and_redis():
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.REDIS_URL)
    try:
        async with session_factory() as session:
            service = HealthService(session, redis)
            health = await service.check()
            database = await service.check_database()
    finally:
        await redis.aclose()
        await engine.dispose()

    assert health.status is HealthStatus.OK
    assert database.status is CheckStatus.OK
    assert database.database
