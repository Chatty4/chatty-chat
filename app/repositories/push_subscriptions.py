import uuid

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PushSubscription


class PushSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_subscription(
        self, user_id: uuid.UUID, endpoint: str, p256dh: str, auth: str
    ) -> None:
        """
        Insert the subscription, or update the one with this endpoint: new keys, and the
        current user as owner. One statement, so two tabs saving at once can't create two rows.
        """
        stmt = insert(PushSubscription).values(
            user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[PushSubscription.endpoint],
            set_={
                "user_id": stmt.excluded.user_id,
                "p256dh": stmt.excluded.p256dh,
                "auth": stmt.excluded.auth,
                # onupdate doesn't run for an upsert, so set it here
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def delete_subscription(self, user_id: uuid.UUID, endpoint: str) -> None:
        """Delete the subscription with this endpoint, only if it belongs to user_id."""
        await self._session.execute(
            delete(PushSubscription).where(
                PushSubscription.endpoint == endpoint,
                PushSubscription.user_id == user_id,
            )
        )
