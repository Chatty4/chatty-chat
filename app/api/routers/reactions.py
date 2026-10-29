import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, ReactionServiceDep
from app.schemas.messages import Emoji, ReactionsResponse

router = APIRouter()


@router.put("/messages/{message_id}/reactions/{emoji}", response_model=ReactionsResponse)
async def add_reaction(
    message_id: uuid.UUID,
    emoji: Emoji,
    user: CurrentUser,
    service: ReactionServiceDep,
) -> ReactionsResponse:
    return await service.add(user, message_id, emoji)
