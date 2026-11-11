from aiokafka import AIOKafkaProducer
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import ChannelEventRepository
from app.schemas.events import ChannelEvent

BATCH_SIZE = 100


async def publish_batch(
    session: AsyncSession,
    events: ChannelEventRepository,
    producer: AIOKafkaProducer,
    topic: str,
) -> int:
    """Sends up to 100 events to Kafka in id order and marks them. Any error rolls back."""
    async with session.begin():
        rows = await events.lock_unpublished(BATCH_SIZE)
        for row in rows:
            event = ChannelEvent.model_validate(row, from_attributes=True)
            await producer.send_and_wait(topic, event, key=event.channel_id)
        await events.mark_published([row.id for row in rows])
    return len(rows)
