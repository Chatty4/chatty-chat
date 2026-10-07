import pytest
from redis.asyncio import Redis

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.schemas.health import CheckStatus, HealthStatus
from app.services.health_service import HealthService

pytestmark = pytest.mark.integration


async def test_health_against_real_db_and_redis():
    redis = Redis.from_url(get_settings().REDIS_URL)
    try:
        async with SessionLocal() as session:
            service = HealthService(session, redis)
            health = await service.check()
            database = await service.check_database()
    finally:
        await redis.aclose()
        await engine.dispose()

    assert health.status is HealthStatus.OK
    assert database.status is CheckStatus.OK
    assert database.database
