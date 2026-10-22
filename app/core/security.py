import uuid
from dataclasses import dataclass
from functools import cache

import jwt

from app.core.config import get_settings
from app.core.exceptions import Unauthorized


@dataclass(frozen=True)
class AccessToken:
    user_id: uuid.UUID
    expires_at: float  # 'exp', seconds since the epoch


@cache
def _public_key() -> str:
    return get_settings().read_jwt_public_key()


def decode_access_token(token: str) -> AccessToken:
    """Check a chatty-core access token and return its user id ('sub') and 'exp'."""
    try:
        payload = jwt.decode(
            token,
            _public_key(),
            algorithms=[get_settings().JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        return AccessToken(uuid.UUID(payload["sub"]), payload["exp"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise Unauthorized() from exc
