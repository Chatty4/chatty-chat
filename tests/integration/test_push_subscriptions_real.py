import asyncio
import base64
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import PushSubscription
from app.repositories import PushSubscriptionRepository
from app.schemas.push import DeletePushSubscriptionRequest, SavePushSubscriptionRequest
from app.services.push_subscription_service import PushSubscriptionService

pytestmark = pytest.mark.integration

ANA, BOB = uuid.uuid4(), uuid.uuid4()


def keys() -> dict[str, str]:
    """Fresh random keys in the browser's format (base64url without padding)."""

    def b64url(size: int) -> str:
        return base64.urlsafe_b64encode(os.urandom(size)).decode().rstrip("=")

    return {"p256dh": b64url(65), "auth": b64url(16)}


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def endpoint(sessions):
    """A fresh endpoint, with its row deleted afterwards."""
    url = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4()}"
    yield url
    async with sessions.begin() as session:
        await session.execute(delete(PushSubscription).where(PushSubscription.endpoint == url))


async def save(sessions, user_id: uuid.UUID, endpoint: str, **key_values: str) -> None:
    request = SavePushSubscriptionRequest(endpoint=endpoint, **(keys() | key_values))
    async with sessions() as session:
        service = PushSubscriptionService(session, PushSubscriptionRepository(session))
        await service.save_subscription(user_id, request)


async def remove(sessions, user_id: uuid.UUID, endpoint: str) -> None:
    async with sessions() as session:
        service = PushSubscriptionService(session, PushSubscriptionRepository(session))
        await service.delete_subscription(user_id, DeletePushSubscriptionRequest(endpoint=endpoint))


async def rows(sessions, endpoint: str) -> list[PushSubscription]:
    async with sessions() as session:
        result = await session.scalars(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )
        return list(result)


async def test_new_endpoint_is_saved(sessions, endpoint):
    new_keys = keys()

    await save(sessions, ANA, endpoint, **new_keys)

    [row] = await rows(sessions, endpoint)
    assert (row.user_id, row.p256dh, row.auth) == (ANA, new_keys["p256dh"], new_keys["auth"])


async def test_saving_again_updates_the_keys_in_the_same_row(sessions, endpoint):
    await save(sessions, ANA, endpoint)
    [first] = await rows(sessions, endpoint)
    new_keys = keys()

    await save(sessions, ANA, endpoint, **new_keys)

    [row] = await rows(sessions, endpoint)
    assert (row.id, row.created_at) == (first.id, first.created_at)
    assert (row.p256dh, row.auth) == (new_keys["p256dh"], new_keys["auth"])
    assert row.updated_at > first.updated_at


async def test_endpoint_saved_by_another_user_moves_to_them(sessions, endpoint):
    await save(sessions, ANA, endpoint)

    await save(sessions, BOB, endpoint)

    [row] = await rows(sessions, endpoint)
    assert row.user_id == BOB


async def test_two_tabs_saving_at_once_leave_one_row(sessions, endpoint):
    await asyncio.gather(save(sessions, ANA, endpoint), save(sessions, ANA, endpoint))

    assert len(await rows(sessions, endpoint)) == 1


async def test_delete_removes_my_subscription_and_is_safe_twice(sessions, endpoint):
    await save(sessions, ANA, endpoint)

    await remove(sessions, ANA, endpoint)
    await remove(sessions, ANA, endpoint)

    assert await rows(sessions, endpoint) == []


async def test_delete_keeps_another_users_subscription(sessions, endpoint):
    await save(sessions, ANA, endpoint)

    await remove(sessions, BOB, endpoint)

    [row] = await rows(sessions, endpoint)
    assert row.user_id == ANA


# Queries of the push worker (CHAT-220)


async def test_list_for_users_returns_only_their_subscriptions(sessions, endpoint):
    owner, other = uuid.uuid4(), uuid.uuid4()
    await save(sessions, owner, endpoint)

    async with sessions() as session:
        repo = PushSubscriptionRepository(session)
        assert [row.endpoint for row in await repo.list_for_users([owner, other])] == [endpoint]
        assert await repo.list_for_users([other]) == []
        assert await repo.list_for_users([]) == []


async def test_delete_by_endpoint_deletes_whoever_owns_it(sessions, endpoint):
    await save(sessions, ANA, endpoint)

    async with sessions.begin() as session:
        await PushSubscriptionRepository(session).delete_by_endpoint(endpoint)

    assert await rows(sessions, endpoint) == []


async def test_only_the_10_newest_subscriptions_are_kept(sessions):
    user_id = uuid.uuid4()
    endpoints = [f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4()}" for _ in range(11)]
    try:
        for url in endpoints:  # one by one: each save is its own transaction, so updated_at grows
            await save(sessions, user_id, url)

        async with sessions() as session:
            kept = await session.scalars(
                select(PushSubscription.endpoint).where(PushSubscription.user_id == user_id)
            )
            assert set(kept) == set(endpoints[1:])  # the first one saved is gone
    finally:
        async with sessions.begin() as session:
            await session.execute(
                delete(PushSubscription).where(PushSubscription.user_id == user_id)
            )
