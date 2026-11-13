import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import PushSubscriptionRepository
from app.schemas.push import DeletePushSubscriptionRequest, SavePushSubscriptionRequest

MAX_SUBSCRIPTIONS = 10


class PushSubscriptionService:
    """Web Push subscriptions of the current user's browsers (api-chat.md, "Push"; D-13)."""

    def __init__(self, session: AsyncSession, subscriptions: PushSubscriptionRepository) -> None:
        self._session = session
        self._subscriptions = subscriptions

    async def save_subscription(
        self, user_id: uuid.UUID, request: SavePushSubscriptionRequest
    ) -> None:
        """
        Save this browser for the user. A known endpoint gets the new keys; one that belongs
        to another user (a shared computer) moves to this user, so a browser only gets pushes
        for whoever is logged in.
        """
        async with self._session.begin():
            await self._subscriptions.save_subscription(
                user_id, request.endpoint, request.p256dh, request.auth
            )
            await self._subscriptions.delete_all_but_newest(user_id, MAX_SUBSCRIPTIONS)

    async def delete_subscription(
        self, user_id: uuid.UUID, request: DeletePushSubscriptionRequest
    ) -> None:
        """
        Forget this browser. Only the user's own subscription is deleted, and nothing to delete
        is not an error: calling it twice is safe and doesn't reveal other users' subscriptions.
        """
        async with self._session.begin():
            await self._subscriptions.delete_subscription(user_id, request.endpoint)
