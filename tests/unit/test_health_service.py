from app.schemas.health import CheckStatus, HealthStatus
from app.services.health_service import HealthService


class FakeResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class FakeSession:
    def __init__(self, database: str = "chat_db", error: Exception | None = None) -> None:
        self._database = database
        self._error = error

    async def execute(self, statement):
        if self._error:
            raise self._error
        return FakeResult(self._database)


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


async def test_check_database_returns_the_database_name():
    result = await HealthService(FakeSession(database="chat_db"), FakeRedis()).check_database()

    assert result.status is CheckStatus.OK
    assert result.database == "chat_db"
    assert result.error is None


async def test_check_database_returns_only_the_error_type():
    session = FakeSession(error=ConnectionRefusedError("secret host details"))

    result = await HealthService(session, FakeRedis()).check_database()

    assert result.status is CheckStatus.ERROR
    assert result.database is None
    assert result.error == "ConnectionRefusedError"
