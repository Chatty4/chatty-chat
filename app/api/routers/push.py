from fastapi import APIRouter, status

from app.api.deps import CurrentUser, PushSubscriptionServiceDep
from app.schemas.push import DeletePushSubscriptionRequest, SavePushSubscriptionRequest

router = APIRouter()


@router.post("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def save_push_subscription(
    body: SavePushSubscriptionRequest, user: CurrentUser, service: PushSubscriptionServiceDep
) -> None:
    await service.save_subscription(user.user_id, body)


@router.delete("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def delete_push_subscription(
    body: DeletePushSubscriptionRequest, user: CurrentUser, service: PushSubscriptionServiceDep
) -> None:
    await service.delete_subscription(user.user_id, body)