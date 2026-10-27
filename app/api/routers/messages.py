import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, MessageServiceDep
from app.schemas.messages import EditMessageRequest, MessageResponse, SendMessageRequest

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


@router.put("/messages/{message_id}", response_model=MessageResponse)
async def edit_message(
    message_id: uuid.UUID,
    data: EditMessageRequest,
    user: CurrentUser,
    service: MessageServiceDep,
) -> MessageResponse:
    return await service.edit(user, message_id, data)
