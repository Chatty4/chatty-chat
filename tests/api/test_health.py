from app.api.deps import get_health_service
from app.schemas.health import CheckStatus, HealthResponse, HealthStatus


class FakeHealthService:
    def __init__(self, health: HealthResponse) -> None:
        self._health = health

    async def check(self) -> HealthResponse:
        return self._health


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


async def test_health_db_endpoint_is_gone(app, client):
    # /health/db returned the database name and exception class to anyone (CHAT-243).
    response = await client.get("/health/db")

    assert response.status_code == 404
