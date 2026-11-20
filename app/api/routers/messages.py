import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, MessageServiceDep
from app.api.rate_limits import LIMIT_DELETE, LIMIT_EDIT, LIMIT_HISTORY, LIMIT_SEND
from app.schemas.messages import (
    AttachmentUrlsResponse,
    EditMessageRequest,
    MessageHistoryResponse,
    MessageResponse,
    SendMessageRequest,
)

router = APIRouter()


@router.post(
    "/channels/{channel_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[LIMIT_SEND],
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


@router.get(
    "/channels/{channel_id}/messages",
    response_model=MessageHistoryResponse,
    dependencies=[LIMIT_HISTORY],
)
async def list_messages(
    channel_id: uuid.UUID,
    user: CurrentUser,
    service: MessageServiceDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    around_seq: Annotated[int | None, Query(ge=1)] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessageHistoryResponse:
    return await service.list_history(
        user, channel_id, before_seq, limit, around_seq=around_seq, after_seq=after_seq
    )


@router.put("/messages/{message_id}", response_model=MessageResponse, dependencies=[LIMIT_EDIT])
async def edit_message(
    message_id: uuid.UUID,
    data: EditMessageRequest,
    user: CurrentUser,
    service: MessageServiceDep,
) -> MessageResponse:
    return await service.edit(user, message_id, data)


@router.delete(
    "/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[LIMIT_DELETE]
)
async def delete_message(
    message_id: uuid.UUID,
    user: CurrentUser,
    service: MessageServiceDep,
) -> None:
    await service.delete(user, message_id)


@router.get("/messages/{message_id}/attachments", response_model=AttachmentUrlsResponse)
async def list_attachment_urls(
    message_id: uuid.UUID,
    user: CurrentUser,
    service: MessageServiceDep,
) -> AttachmentUrlsResponse:
    return await service.attachment_urls(user, message_id)
