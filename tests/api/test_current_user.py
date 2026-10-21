import uuid

import pytest

from app.api.deps import CurrentUser, get_core_client
from app.core.exceptions import ServiceUnavailable
from tests.fakes import FakeCore, memberships

USER_ID = uuid.uuid4()


@pytest.fixture
def protected(app):
    """A test-only route behind CurrentUser, until CHAT-177 adds the first real one."""

    @app.get("/test/me")
    async def me(user: CurrentUser) -> dict:
        return {"user_id": str(user.user_id)}


def use_core(app, core: FakeCore) -> None:
    app.dependency_overrides[get_core_client] = lambda: core


async def test_no_token_is_401(app, client, protected):
    # FastAPI builds the core client before the token check, so it must be faked here too
    use_core(app, FakeCore())

    response = await client.get("/test/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_bad_token_is_401(app, client, protected, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))

    response = await client.get("/test/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_inactive_user_is_403(app, client, protected, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID, is_active=False)))
    token = make_token(sub=str(USER_ID))

    response = await client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "user_inactive"


async def test_core_unavailable_is_503(app, client, protected, make_token):
    use_core(app, FakeCore(error=ServiceUnavailable()))
    token = make_token(sub=str(USER_ID))

    response = await client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "core_unavailable"


async def test_valid_token_reaches_the_route(app, client, protected, make_token):
    use_core(app, FakeCore(memberships=memberships(USER_ID)))
    token = make_token(sub=str(USER_ID))

    response = await client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER_ID)}
