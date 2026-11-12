import asyncio
import random
import uuid
from collections.abc import AsyncGenerator

import pytest
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.structs import ConsumerRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.clients.kafka import create_consumer, create_producer
from app.core.config import get_settings
from app.jobs.relay import asyncpg_connection, take_relay_lock
from app.models import ChannelEvent
from app.repositories import ChannelEventRepository
from app.schemas.events import EventType
from app.services.outbox_relay import publish_batch

pytestmark = pytest.mark.integration


@pytest.fixture
async def topic() -> AsyncGenerator[str]:
    """One partition, so the records come back in the order they were sent. Never chat.events."""
    name = f"chat.test-{uuid.uuid4().hex}"
    admin = AIOKafkaAdminClient(bootstrap_servers=get_settings().KAFKA_BOOTSTRAP_SERVERS)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(name, num_partitions=1, replication_factor=1)])
        try:
            yield name
        finally:
            await admin.delete_topics([name])
    finally:
        await admin.close()


def key_and_seq(record: ConsumerRecord[bytes, ChannelEvent | None]) -> tuple[bytes, int]:
    assert record.key is not None, "the record has no key"
    assert record.value is not None, "the record isn't a channel event"
    return record.key, record.value.seq


async def test_only_one_connection_holds_the_relay_lock():
    # its own lock id, so a relay running against the same database doesn't matter
    lock_id = random.randint(1_000_000, 9_000_000)
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        async with engine.connect() as first, engine.connect() as second:
            a, b = await asyncpg_connection(first), await asyncpg_connection(second)

            assert await take_relay_lock(a, lock_id) is True
            assert await take_relay_lock(b, lock_id) is False

            await a.fetchval("SELECT pg_advisory_unlock($1)", lock_id)
            assert await take_relay_lock(b, lock_id) is True
            await b.fetchval("SELECT pg_advisory_unlock($1)", lock_id)
    finally:
        await engine.dispose()


async def test_a_batch_reaches_kafka_in_order_keyed_by_channel_and_is_marked(outbox_db, topic):
    a, b = uuid.uuid4(), uuid.uuid4()
    async with outbox_db.begin() as session:
        session.add_all(
            ChannelEvent(channel_id=channel_id, seq=seq, type=EventType.MESSAGE_CREATED, payload={})
            for channel_id, seq in [(a, 1), (b, 1), (a, 2)]
        )
    producer = create_producer()
    consumer = create_consumer(f"test-{uuid.uuid4().hex}", topic)
    await producer.start()
    await consumer.start()
    try:
        async with outbox_db() as session:
            events = ChannelEventRepository(session)
            assert await publish_batch(session, events, producer, topic) == 3
            assert await publish_batch(session, events, producer, topic) == 0  # nothing left

        async with asyncio.timeout(10):
            records = [await consumer.getone() for _ in range(3)]
    finally:
        await consumer.stop()
        await producer.stop()

    assert [key_and_seq(record) for record in records] == [
        (str(a).encode(), 1),
        (str(b).encode(), 1),
        (str(a).encode(), 2),
    ]
    async with outbox_db() as session:
        published = await session.scalars(select(ChannelEvent.published_at))
        assert None not in published.all()
