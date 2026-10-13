import logging

from app.schemas.health import CheckStatus, HealthStatus
from app.services.health_service import HealthService


class FakeSession:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def execute(self, statement) -> None:
        if self._error:
            raise self._error


class FakeRedis:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def ping(self) -> bool:
        if self._error:
            raise self._error
        return True


async def test_check_is_ok_when_db_and_redis_answer():
    result = await HealthService(FakeSession(), FakeRedis()).check()

    assert result.status is HealthStatus.OK
    assert result.db is CheckStatus.OK
    assert result.redis is CheckStatus.OK


async def test_check_is_degraded_when_db_fails():
    session = FakeSession(error=ConnectionRefusedError())

    result = await HealthService(session, FakeRedis()).check()

    assert result.status is HealthStatus.DEGRADED
    assert result.db is CheckStatus.ERROR
    assert result.redis is CheckStatus.OK


async def test_check_is_degraded_when_redis_fails():
    redis = FakeRedis(error=ConnectionError())

    result = await HealthService(FakeSession(), redis).check()

    assert result.status is HealthStatus.DEGRADED
    assert result.db is CheckStatus.OK
    assert result.redis is CheckStatus.ERROR


async def test_check_logs_the_error_instead_of_returning_it(caplog):
    session = FakeSession(error=ConnectionRefusedError("secret host details"))

    with caplog.at_level(logging.WARNING):
        result = await HealthService(session, FakeRedis()).check()

    assert "secret host details" not in result.model_dump_json()
    assert "Health check failed: db" in caplog.text
    assert "secret host details" in caplog.text
