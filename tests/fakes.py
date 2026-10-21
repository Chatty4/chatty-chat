import uuid

from app.schemas.core import CoreChannel, Memberships, TeamMembership


def memberships(
    user_id: uuid.UUID, *, is_active: bool = True, teams: list[TeamMembership] | None = None
) -> Memberships:
    return Memberships(user_id=user_id, is_active=is_active, teams=teams or [], channels=[])


def channel(*members: uuid.UUID, archived: bool = False, team_deleted: bool = False) -> CoreChannel:
    return CoreChannel.model_validate(
        {
            "channel_id": uuid.uuid4(),
            "team_id": uuid.uuid4(),
            "kind": "public",
            "archived": archived,
            "team_deleted": team_deleted,
            "members": [
                {"user_id": m, "role": "member", "notify_level": "mentions", "muted": False}
                for m in members
            ],
        }
    )


class FakeCore:
    """Stands in for CoreClient: returns what it was given, or raises `error`."""

    def __init__(
        self,
        memberships: Memberships | None = None,
        channel: CoreChannel | None = None,
        error: Exception | None = None,
    ) -> None:
        self._memberships = memberships
        self._channel = channel
        self._error = error

    async def get_memberships(self, user_id: uuid.UUID) -> Memberships | None:
        if self._error:
            raise self._error
        return self._memberships

    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        if self._error:
            raise self._error
        return self._channel
