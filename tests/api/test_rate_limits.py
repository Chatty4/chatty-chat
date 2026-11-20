import importlib
import pkgutil
import uuid

import pytest
from fastapi.routing import APIRoute

from app.api import routers
from app.api.deps import get_core_client, get_rate_limiter
from tests.fakes import FakeCore, FakeRateLimiter, memberships

USER_ID, CHANNEL_ID, MESSAGE_ID, TEAM_ID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
API = "/api/chat/v1"

# api-chat.md, Rate limits: route (without /api/chat/v1) -> (limit name, requests per minute).
# Every other route has none.
LIMITS = {
    ("POST", "/channels/{channel_id}/messages"): ("send", 60),
    ("PUT", "/messages/{message_id}"): ("edit", 30),
    ("DELETE", "/messages/{message_id}"): ("delete", 30),
    ("GET", "/channels/{channel_id}/messages"): ("history", 120),
    ("GET", "/channels/{channel_id}/events"): ("events", 120),
    ("POST", "/sync"): ("sync", 20),
    ("PUT", "/messages/{message_id}/reactions/{emoji}"): ("reactions", 60),
    ("DELETE", "/messages/{message_id}/reactions/{emoji}"): ("reactions", 60),
    ("POST", "/channels/{channel_id}/pins/{message_id}"): ("pins", 20),
    ("DELETE", "/channels/{channel_id}/pins/{message_id}"): ("pins", 20),
    ("POST", "/channels/{channel_id}/read"): ("read", 120),
    ("GET", "/search"): ("search", 30),
    ("POST", "/push/subscriptions"): ("push", 10),
    ("DELETE", "/push/subscriptions"): ("push", 10),
}

# one real request per limited route; the limit is checked before the body and the query
REQUESTS = [
    ("POST", f"{API}/channels/{CHANNEL_ID}/messages", "send", 60),
    ("PUT", f"{API}/messages/{MESSAGE_ID}", "edit", 30),
    ("DELETE", f"{API}/messages/{MESSAGE_ID}", "delete", 30),
    ("GET", f"{API}/channels/{CHANNEL_ID}/messages", "history", 120),
    ("GET", f"{API}/channels/{CHANNEL_ID}/events?after_seq=0", "events", 120),
    ("POST", f"{API}/sync", "sync", 20),
    ("PUT", f"{API}/messages/{MESSAGE_ID}/reactions/%F0%9F%91%8D", "reactions", 60),
    ("DELETE", f"{API}/messages/{MESSAGE_ID}/reactions/%F0%9F%91%8D", "reactions", 60),
    ("POST", f"{API}/channels/{CHANNEL_ID}/pins/{MESSAGE_ID}", "pins", 20),
    ("DELETE", f"{API}/channels/{CHANNEL_ID}/pins/{MESSAGE_ID}", "pins", 20),
    ("POST", f"{API}/channels/{CHANNEL_ID}/read", "read", 120),
    ("GET", f"{API}/search?team_id={TEAM_ID}&q=deploy", "search", 30),
    ("POST", f"{API}/push/subscriptions", "push", 10),
    ("DELETE", f"{API}/push/subscriptions", "push", 10),
]


def route_limits() -> dict[tuple[str, str], tuple[str, int]]:
    """The rate limit on each route of app/api/routers, read from its dependencies."""
    found = {}
    modules = (
        importlib.import_module(f"{routers.__name__}.{m.name}")
        for m in pkgutil.iter_modules(routers.__path__)
    )
    routes = [route for module in modules for route in module.router.routes]
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        for depends in route.dependencies:
            limit = getattr(depends.dependency, "rate_limit", None)
            if limit is not None:
                name, per_window, window = limit
                assert window == 60, f"{route.path}: the contract counts per minute"
                methods = route.methods or set()  # typed Optional; always set on an APIRoute
                found |= {(method, route.path): (name, per_window) for method in methods}
    return found


@pytest.fixture
def auth(app, make_token) -> dict[str, str]:
    app.dependency_overrides[get_core_client] = lambda: FakeCore(memberships=memberships(USER_ID))
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


@pytest.fixture
def limiter(app) -> FakeRateLimiter:
    """Refuses every request with Retry-After 17."""
    fake = FakeRateLimiter(retry_after=17)
    app.dependency_overrides[get_rate_limiter] = lambda: fake
    return fake


def test_the_limited_routes_and_their_limits_are_the_contracts():
    assert route_limits() == LIMITS


def test_every_limited_route_has_a_request_below():
    assert {(method, name) for method, _, name, _ in REQUESTS} == {
        (method, name) for (method, _), (name, _) in LIMITS.items()
    }


@pytest.mark.parametrize(
    ("method", "path", "name", "limit"),
    REQUESTS,
    ids=[f"{method} {name}" for method, _, name, _ in REQUESTS],
)
async def test_over_the_limit_is_429_with_retry_after(
    client, auth, limiter, method, path, name, limit
):
    response = await client.request(method, path, headers=auth)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert response.json()["error"]["code"] == "rate_limited"
    assert limiter.hits == [(name, USER_ID, limit, 60)]


async def test_without_a_token_is_401_and_not_counted(client, auth, limiter):
    response = await client.post(f"{API}/channels/{CHANNEL_ID}/messages", json={})

    assert response.status_code == 401
    assert limiter.hits == []
