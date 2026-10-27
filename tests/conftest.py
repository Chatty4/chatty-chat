import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from app.core import security
from app.main import create_app

# Fallback values for environments without a .env (e.g. CI). Only without one: pydantic-settings
# ranks environment variables above .env, so setting them would hide a local .env.
# Real environment variables (docker compose) still win because setdefault only fills gaps.
if not Path(".env").exists():  # the same relative path Settings reads
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


def _key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.fixture(scope="session")
def test_keys() -> tuple[str, str]:
    """A test RSA key pair, never the real keys."""
    return _key_pair()


@pytest.fixture(scope="session")
def other_private_key() -> str:
    return _key_pair()[0]


@pytest.fixture
def make_token(test_keys, monkeypatch):
    """Signs a token like chatty-core does. A claim set to None is left out."""
    private, public = test_keys
    monkeypatch.setattr(security, "_public_key", lambda: public)

    def make(key: str = private, **claims) -> str:
        now = datetime.now(UTC)
        payload = {"sub": str(uuid.uuid4()), "iat": now, "exp": now + timedelta(minutes=5)}
        payload |= claims
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(payload, key, algorithm="RS256")

    return make
