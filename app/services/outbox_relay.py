import asyncio

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
    """Sends up to 100 events to Kafka in id order and marks them. No transaction is open
    while it waits on Kafka, so a delete never waits for the relay."""
    async with session.begin():
        rows = await events.list_unpublished(BATCH_SIZE)
        batch = [(row.id, ChannelEvent.model_validate(row, from_attributes=True)) for row in rows]
    if not batch:
        return 0

    queued = []
    try:
        for _, event in batch:
            # send() only queues the record, in order; its future ends when Kafka acknowledges it
            queued.append(await producer.send(topic, event, key=event.channel_id))
    finally:
        # every queued record is waited for, so a failure leaves none of them behind
        results = await asyncio.gather(*queued, return_exceptions=True)

    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise errors[0]

    async with session.begin():
        await events.mark_published([row_id for row_id, _ in batch])
    return len(batch)
