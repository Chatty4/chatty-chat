import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.clients.kafka import create_consumer, create_producer
from app.core.config import get_settings
from app.schemas.events import ChannelEvent, EventType

pytestmark = pytest.mark.integration


@pytest.fixture
async def topic() -> AsyncGenerator[str]:
    """A throwaway topic: never chat.events, which the push worker reads from the start."""
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


async def test_an_event_round_trips_with_its_channel_as_key(topic):
    event = ChannelEvent(
        type=EventType.MESSAGE_CREATED,
        channel_id=uuid.uuid4(),
        seq=1,
        created_at=datetime.now(UTC),
        payload={"message": {"body": "hi"}},
    )
    producer = create_producer()
    consumer = create_consumer(f"test-{uuid.uuid4().hex}", topic)
    await producer.start()
    await consumer.start()
    try:
        await producer.send_and_wait(topic, event, key=event.channel_id)
        async with asyncio.timeout(10):
            record = await consumer.getone()
    finally:
        await consumer.stop()
        await producer.stop()

    assert record.key == str(event.channel_id).encode()
    assert record.value == event
