import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ServiceUnavailable
from app.jobs import push_worker
from app.jobs.push_worker import handle_until_done
from app.schemas.events import ChannelEvent
from app.services.push_service import PushService

EVENT = ChannelEvent(
    type="message.created",
    channel_id=uuid.uuid4(),
    seq=1,
    created_at=datetime.now(UTC),
    payload={},
)


class FlakyService:
    """Raises the given errors in order, then succeeds."""

    def __init__(self, *errors: Exception) -> None:
        self._errors = list(errors)
        self.calls = 0

    async def handle(self, event: ChannelEvent) -> None:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(push_worker, "RETRY_SECONDS", 0)


async def test_retries_the_same_event_until_it_works():
    service = FlakyService(
        ServiceUnavailable(), OperationalError("SELECT", {}, Exception()), RedisError()
    )

    await handle_until_done(cast(PushService, service), EVENT)

    assert service.calls == 4


async def test_a_bug_is_not_retried():
    service = FlakyService(TypeError("bug"))

    with pytest.raises(TypeError):
        await handle_until_done(cast(PushService, service), EVENT)
    assert service.calls == 1
