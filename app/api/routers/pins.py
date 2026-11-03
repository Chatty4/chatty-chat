import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, PinServiceDep
from app.schemas.pins import PinResponse, PinsResponse

router = APIRouter()


@router.post(
    "/channels/{channel_id}/pins/{message_id}",
    response_model=PinResponse,
    status_code=status.HTTP_201_CREATED,
)
async def pin_message(
    channel_id: uuid.UUID,
    message_id: uuid.UUID,
    user: CurrentUser,
    service: PinServiceDep,
    response: Response,
) -> PinResponse:
    pin, created = await service.pin(user, channel_id, message_id)
    if not created:
        response.status_code = status.HTTP_200_OK  # it was already pinned
    return pin


@router.delete("/channels/{channel_id}/pins/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unpin_message(
    channel_id: uuid.UUID, message_id: uuid.UUID, user: CurrentUser, service: PinServiceDep
) -> None:
    await service.unpin(user, channel_id, message_id)


@router.get("/channels/{channel_id}/pins", response_model=PinsResponse)
async def list_pins(
    channel_id: uuid.UUID, user: CurrentUser, service: PinServiceDep
) -> PinsResponse:
    return await service.list_pins(user, channel_id)
