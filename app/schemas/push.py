"""Request bodies for Web Push subscriptions (api-chat.md#push, D-13)."""

import base64
import binascii
import re
import uuid
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, Field

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")

PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
PUSH_HOST_SUFFIX = ".notify.windows.com"


def check_https_url(value: str) -> str:
    """The push service URL from the browser: https on a known push service. Kept as sent."""
    url = urlsplit(value)
    host = url.hostname or ""  # lowercased; drops user:pass@ and the port
    if url.scheme != "https" or not (host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIX)):
        raise ValueError("not a known push service")
    return value


def _decode_base64url(value: str) -> bytes:
    """base64url with or without padding. Plain base64 (+ and /) is rejected."""
    if not _BASE64URL.fullmatch(value):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except binascii.Error:
        raise ValueError("not base64url") from None


def check_p256dh(value: str) -> str:
    """The browser's P-256 public key: 65 bytes once decoded."""
    if len(_decode_base64url(value)) != 65:
        raise ValueError("must be 65 bytes")
    return value


def check_auth(value: str) -> str:
    """The shared secret: 16 bytes once decoded."""
    if len(_decode_base64url(value)) != 16:
        raise ValueError("must be 16 bytes")
    return value


Endpoint = Annotated[str, Field(max_length=2048), AfterValidator(check_https_url)]
P256dh = Annotated[str, Field(max_length=100), AfterValidator(check_p256dh)]
Auth = Annotated[str, Field(max_length=50), AfterValidator(check_auth)]


class SavePushSubscriptionRequest(BaseModel):
    endpoint: Endpoint
    p256dh: P256dh
    auth: Auth


class DeletePushSubscriptionRequest(BaseModel):
    endpoint: Endpoint


class PushMessage(BaseModel):
    """The fields of message.created's payload.message the push worker needs (events.md)."""

    model_config = {"extra": "ignore"}

    id: uuid.UUID
    author_id: uuid.UUID
    body: str | None = None
    mentions: list[uuid.UUID] = []


class PushNotification(BaseModel):
    """What a browser receives (api-chat.md#push-payload)."""

    type: Literal["message"] = "message"
    team_id: uuid.UUID
    channel_id: uuid.UUID
    message_id: uuid.UUID
    seq: int
    mentioned: bool
    preview: str
