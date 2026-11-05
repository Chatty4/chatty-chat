import uuid

from app.clients.presence import PresenceStore
from app.schemas.core import Memberships
from app.schemas.presence import PresenceResponse
from app.services.access_service import AccessService


class PresenceService:
    def __init__(self, presence: PresenceStore, access: AccessService) -> None:
        self._presence = presence
        self._access = access

    async def list_online(self, user: Memberships, team_id: uuid.UUID) -> PresenceResponse:
        """Who is online in a team. Goes with the presence messages on the WebSocket."""
        self._access.check_in_team(user, team_id)
        return PresenceResponse(team_id=team_id, online=await self._presence.list_online(team_id))
