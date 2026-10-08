import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

# Fallback values for environments without a .env (e.g. unit-test runs without Docker).
# Real values from docker compose or .env take precedence because setdefault only fills gaps.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test")
os.environ.setdefault("CORE_SERVICE_TOKEN", "test-token")


@pytest.fixture
def app():
    application = create_app()
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
