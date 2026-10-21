import uuid
from functools import cache

import jwt

from app.core.config import get_settings
from app.core.exceptions import Unauthorized


@cache
def _public_key() -> str:
    return get_settings().read_jwt_public_key()


def decode_access_token(token: str) -> uuid.UUID:
    """Check a chatty-core access token and return the user id from `sub`."""
    try:
        payload = jwt.decode(
            token,
            _public_key(),
            algorithms=[get_settings().JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise Unauthorized() from exc
