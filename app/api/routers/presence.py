import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, PresenceServiceDep
from app.schemas.presence import PresenceResponse

router = APIRouter()


@router.get("/teams/{team_id}/presence", response_model=PresenceResponse)
async def get_presence(
    team_id: uuid.UUID, user: CurrentUser, service: PresenceServiceDep
) -> PresenceResponse:
    return await service.list_online(user, team_id)
