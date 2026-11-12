import asyncio
import logging
import uuid

import requests
from pywebpush import WebPushException, webpush
from redis.asyncio import Redis

from app.models import PushSubscription

logger = logging.getLogger(__name__)

GONE = {404, 410}  # the browser unsubscribed or the subscription expired
TIMEOUT_SECONDS = 5
TTL_SECONDS = 3600  # how long the push service keeps it for a browser that is offline
DEDUPE_TTL_SECONDS = 86_400  # far longer than any redelivery after a crash or rebalance


def dedupe_key(message_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"push:{message_id}:{user_id}"


class PushDedupe:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def claim(self, message_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """True only the first time for this message and user. Raises RedisError."""
        key = dedupe_key(message_id, user_id)
        return bool(await self._redis.set(key, "1", nx=True, ex=DEDUPE_TTL_SECONDS))


class WebPushClient:
    def __init__(self, private_key: str, subject: str) -> None:
        self._private_key = private_key
        self._subject = subject

    async def send(self, subscription: PushSubscription, data: str) -> bool:
        """
        False when the push service says the subscription is gone. Other failures are logged
        and count as sent: retrying one browser would hold up everyone else's pushes.
        Never logs the endpoint or the keys (api-chat.md#push).
        """
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=data,
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},  # new dict each time: webpush adds to it
                ttl=TTL_SECONDS,
                timeout=TIMEOUT_SECONDS,
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in GONE:
                return False
            logger.warning(
                "Push failed with status %s for subscription %s", status, subscription.id
            )
        except requests.RequestException:
            logger.warning("Push service unreachable for subscription %s", subscription.id)
        return True
