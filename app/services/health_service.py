import asyncio
import logging
from collections.abc import Sequence

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.health import CheckStatus, HealthResponse, HealthStatus

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 2
PING_QUERY = text("SELECT 1")


class HealthService:
    def __init__(self, session: AsyncSession, redis: Redis, tasks: Sequence[asyncio.Task]) -> None:
        self._session = session
        self._redis = redis
        self._tasks = tasks

    async def check(self) -> HealthResponse:
        db, redis = await asyncio.gather(self._check_database(), self._check_redis())
        tasks = self._check_tasks()
        healthy = CheckStatus.ERROR not in (db, redis, tasks)
        return HealthResponse(
            status=HealthStatus.OK if healthy else HealthStatus.DEGRADED,
            db=db,
            redis=redis,
            tasks=tasks,
        )

    def _check_tasks(self) -> CheckStatus:
        """The gateway and the core-event listener run until shutdown; done means crashed."""
        return CheckStatus.ERROR if any(task.done() for task in self._tasks) else CheckStatus.OK

    async def _check_database(self) -> CheckStatus:
        try:
            await asyncio.wait_for(self._session.execute(PING_QUERY), TIMEOUT_SECONDS)
        except Exception:
            # The details go to the log only; the response just says "error".
            logger.warning("Health check failed: db", exc_info=True)
            return CheckStatus.ERROR
        return CheckStatus.OK

    async def _check_redis(self) -> CheckStatus:
        try:
            await asyncio.wait_for(self._redis.ping(), TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Health check failed: redis", exc_info=True)
            return CheckStatus.ERROR
        return CheckStatus.OK
