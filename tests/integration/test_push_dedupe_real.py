import uuid

import pytest
from redis.asyncio import Redis

from app.clients.web_push import DEDUPE_TTL_SECONDS, PushDedupe, dedupe_key
from app.core.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


async def test_only_the_first_claim_wins_and_the_key_expires(redis):
    message_id, user_id, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    dedupe = PushDedupe(redis)
    try:
        assert await dedupe.claim(message_id, user_id)
        assert not await dedupe.claim(message_id, user_id)
        assert await dedupe.claim(message_id, other)  # one claim per user, not per message

        assert 0 < await redis.ttl(dedupe_key(message_id, user_id)) <= DEDUPE_TTL_SECONDS
    finally:
        await redis.delete(dedupe_key(message_id, user_id), dedupe_key(message_id, other))
