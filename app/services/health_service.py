import asyncio

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.health import CheckStatus, DatabaseHealthResponse, HealthResponse, HealthStatus

TIMEOUT_SECONDS = 2
DB_INFO_QUERY = text("SELECT current_database()")


class HealthService:
    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        self._session = session
        self._redis = redis

    async def check(self) -> HealthResponse:
        db, redis = await asyncio.gather(self.check_database(), self._check_redis())
        healthy = db.status is CheckStatus.OK and redis is CheckStatus.OK
        return HealthResponse(
            status=HealthStatus.OK if healthy else HealthStatus.DEGRADED,
            db=db.status,
            redis=redis,
        )

    async def check_database(self) -> DatabaseHealthResponse:
        try:
            result = await asyncio.wait_for(self._session.execute(DB_INFO_QUERY), TIMEOUT_SECONDS)
        except Exception as exc:
            return DatabaseHealthResponse(status=CheckStatus.ERROR, error=type(exc).__name__)
        return DatabaseHealthResponse(status=CheckStatus.OK, database=result.scalar_one())

    async def _check_redis(self) -> CheckStatus:
        try:
            await asyncio.wait_for(self._redis.ping(), TIMEOUT_SECONDS)
        except Exception:
            return CheckStatus.ERROR
        return CheckStatus.OK
