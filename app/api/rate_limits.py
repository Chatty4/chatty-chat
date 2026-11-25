from typing import Any

from fastapi import Depends

from app.api.deps import CurrentUser, RateLimiterDep
from app.core.exceptions import RateLimited


def rate_limit(name: str, limit: int, window: int = 60) -> Any:
    """A route dependency: at most `limit` requests per user in `window` seconds, else 429.
    Routes that share a limit share its `name` (api-chat.md)."""

    async def check(user: CurrentUser, limiter: RateLimiterDep) -> None:
        retry_after = await limiter.hit(name, user.user_id, limit, window)
        if retry_after:
            raise RateLimited(retry_after)

    check.rate_limit = (name, limit, window)  # type: ignore[attr-defined]  # read by the tests
    return Depends(check)


# api-chat.md, Rate limits: per user, per minute
LIMIT_SEND = rate_limit("send", 60)
LIMIT_EDIT = rate_limit("edit", 30)
LIMIT_DELETE = rate_limit("delete", 30)
LIMIT_HISTORY = rate_limit("history", 120)
LIMIT_EVENTS = rate_limit("events", 120)
LIMIT_SYNC = rate_limit("sync", 20)
LIMIT_REACTIONS = rate_limit("reactions", 60)  # add and remove together
LIMIT_PINS = rate_limit("pins", 20)  # pin and unpin together
LIMIT_READ = rate_limit("read", 120)
LIMIT_SEARCH = rate_limit("search", 30)
LIMIT_SEARCH_ATTACHMENTS = rate_limit("search_attachments", 30)
LIMIT_PUSH = rate_limit("push", 10)  # save and delete together
