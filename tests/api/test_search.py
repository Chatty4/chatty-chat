import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.deps import get_core_client, get_search_service
from app.schemas.core import TeamMembership, TeamRole
from app.schemas.search import AttachmentSearchResponse, SearchResponse
from tests.fakes import FakeCore, memberships

URL = "/api/chat/v1/search"
USER_ID, TEAM = uuid.uuid4(), uuid.uuid4()


class FakeSearchService:
    """Records the arguments the router passed and returns an empty page."""

    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()

    async def search(self, *args: Any) -> SearchResponse:
        self.args = args
        return SearchResponse(results=[], next_cursor=None)

    async def search_attachments(self, *args: Any) -> AttachmentSearchResponse:
        self.args = args
        return AttachmentSearchResponse(results=[], next_cursor=None)


@pytest.fixture
def service(app) -> FakeSearchService:
    team = TeamMembership(team_id=TEAM, role=TeamRole.MEMBER, public_channel_ids=[])
    core = FakeCore(memberships=memberships(USER_ID, teams=[team]))
    fake = FakeSearchService()
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_search_service] = lambda: fake
    return fake


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_search_is_200_with_results_and_next_cursor(client, auth, service):
    response = await client.get(URL, params={"team_id": str(TEAM), "q": "deploy"}, headers=auth)

    assert response.status_code == 200
    assert response.json() == {"results": [], "next_cursor": None}


async def test_in_and_from_reach_the_service_as_channel_and_author(client, auth, service):
    channel_id, author_id = uuid.uuid4(), uuid.uuid4()
    params = {
        "team_id": str(TEAM),
        "q": "deploy",
        "in": str(channel_id),
        "from": str(author_id),
        "after": "2026-10-05T14:00:00Z",
        "cursor": "abc",
    }

    await client.get(URL, params=params, headers=auth)

    _user, team_id, q, channel, author, before, after, cursor, limit = service.args
    assert (team_id, q, channel, author) == (TEAM, "deploy", channel_id, author_id)
    assert (before, after) == (None, datetime(2026, 10, 5, 14, tzinfo=UTC))
    assert (cursor, limit) == ("abc", 20)


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"q": "deploy"}, {"team_id": "required"}),
        ({"team_id": "x", "q": "deploy"}, {"team_id": "invalid"}),
        ({}, {"q": "required"}),
        ({"q": "a"}, {"q": "too_short"}),
        ({"q": "a" * 201}, {"q": "too_long"}),
        ({"q": "deploy", "limit": 0}, {"limit": "invalid"}),
        ({"q": "deploy", "limit": 51}, {"limit": "invalid"}),
        ({"q": "deploy", "in": "x"}, {"in": "invalid"}),
        ({"q": "deploy", "before": "2026-10-05T14:00:00"}, {"before": "invalid"}),
    ],
)
async def test_invalid_query_is_400(client, auth, service, params, fields):
    params = {"team_id": str(TEAM)} | params if "team_id" not in fields else params

    response = await client.get(URL, params=params, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)


async def test_no_token_is_401(client, service):
    response = await client.get(URL, params={"team_id": str(TEAM), "q": "deploy"})

    assert response.status_code == 401


FILES_URL = f"{URL}/attachments"


async def test_search_attachments_passes_the_same_query_to_the_service(client, auth, service):
    channel_id = uuid.uuid4()
    params = {"team_id": str(TEAM), "q": "orange", "in": str(channel_id), "limit": 5}

    response = await client.get(FILES_URL, params=params, headers=auth)

    assert response.status_code == 200
    assert response.json() == {"results": [], "next_cursor": None}
    _user, team_id, q, channel, _author, _before, _after, _cursor, limit = service.args
    assert (team_id, q, channel, limit) == (TEAM, "orange", channel_id, 5)


async def test_search_attachments_without_q_lists_files(client, auth, service):
    response = await client.get(FILES_URL, params={"team_id": str(TEAM)}, headers=auth)

    assert response.status_code == 200
    assert service.args[2] is None  # q


async def test_search_attachments_checks_the_query_like_search(client, auth, service):
    response = await client.get(FILES_URL, params={"team_id": str(TEAM), "q": "a"}, headers=auth)

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"q": "too_short"}
