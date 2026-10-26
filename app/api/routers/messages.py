import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, MessageServiceDep
from app.schemas.messages import MessageResponse, SendMessageRequest

router = APIRouter()


@router.post(
    "/channels/{channel_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    channel_id: uuid.UUID,
    data: SendMessageRequest,
    user: CurrentUser,
    service: MessageServiceDep,
    response: Response,
) -> MessageResponse:
    message, created = await service.send(user, channel_id, data)
    if not created:
        response.status_code = status.HTTP_200_OK  # a retry returns the original
    return message
