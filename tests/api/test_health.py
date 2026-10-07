from app.api.deps import get_health_service
from app.schemas.health import CheckStatus, DatabaseHealthResponse, HealthResponse, HealthStatus


class FakeHealthService:
    def __init__(
        self,
        health: HealthResponse | None = None,
        database: DatabaseHealthResponse | None = None,
    ) -> None:
        self._health = health
        self._database = database

    async def check(self) -> HealthResponse:
        return self._health

    async def check_database(self) -> DatabaseHealthResponse:
        return self._database


def use_fake(app, **results) -> None:
    app.dependency_overrides[get_health_service] = lambda: FakeHealthService(**results)


async def test_health_returns_200_when_db_and_redis_are_ok(app, client):
    use_fake(
        app,
        health=HealthResponse(status=HealthStatus.OK, db=CheckStatus.OK, redis=CheckStatus.OK),
    )

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}


async def test_health_returns_503_when_db_is_down(app, client):
    use_fake(
        app,
        health=HealthResponse(
            status=HealthStatus.DEGRADED, db=CheckStatus.ERROR, redis=CheckStatus.OK
        ),
    )

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "error", "redis": "ok"}


async def test_health_db_returns_database_name(app, client):
    use_fake(app, database=DatabaseHealthResponse(status=CheckStatus.OK, database="chat_db"))

    response = await client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "chat_db", "error": None}


async def test_health_db_returns_503_with_error_type(app, client):
    use_fake(
        app,
        database=DatabaseHealthResponse(status=CheckStatus.ERROR, error="ConnectionRefusedError"),
    )

    response = await client.get("/health/db")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "database": None,
        "error": "ConnectionRefusedError",
    }
