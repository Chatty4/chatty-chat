import uuid
from typing import cast

import pytest

from app.api.deps import get_core_client, get_presence_service
from app.clients.core import CoreClient
from app.clients.presence import PresenceStore
from app.schemas.core import TeamMembership, TeamRole
from app.services.access_service import AccessService
from app.services.presence_service import PresenceService
from tests.fakes import FakeCore, FakePresence, memberships

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
TEAM_ID = uuid.uuid4()


def url(team_id: uuid.UUID | str) -> str:
    return f"/api/chat/v1/teams/{team_id}/presence"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


@pytest.fixture
def presence(app) -> FakePresence:
    """USER_ID is in TEAM_ID only."""
    team = TeamMembership(team_id=TEAM_ID, role=TeamRole.MEMBER, public_channel_ids=[])
    core = FakeCore(memberships=memberships(USER_ID, teams=[team]))
    presence = FakePresence()
    service = PresenceService(cast(PresenceStore, presence), AccessService(cast(CoreClient, core)))
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_presence_service] = lambda: service
    return presence


async def test_presence_is_200_with_the_online_users(client, auth, presence):
    presence.online[TEAM_ID] = [USER_ID, OTHER_ID]

    response = await client.get(url(TEAM_ID), headers=auth)

    assert response.status_code == 200
    assert response.json() == {"team_id": str(TEAM_ID), "online": [str(USER_ID), str(OTHER_ID)]}


async def test_a_team_with_nobody_online_is_an_empty_list(client, auth, presence):
    response = await client.get(url(TEAM_ID), headers=auth)

    assert response.status_code == 200
    assert response.json()["online"] == []


async def test_a_team_the_user_isnt_in_is_403_and_shows_nobody(client, auth, presence):
    other_team = uuid.uuid4()
    presence.online[other_team] = [OTHER_ID]

    response = await client.get(url(other_team), headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_team_member"


async def test_a_team_id_that_isnt_a_uuid_is_400(client, auth, presence):
    response = await client.get(url("not-a-uuid"), headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", {"team_id": "invalid"})


async def test_without_a_token_is_401(client, presence):
    response = await client.get(url(TEAM_ID))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
