import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelEvent as ChannelEventRow
from app.repositories import ChannelEventRepository
from app.services.outbox_relay import BATCH_SIZE, publish_batch
from tests.fakes import FakeOutbox, FakeProducer, FakeSession


def row(row_id: int, channel_id: uuid.UUID, seq: int) -> ChannelEventRow:
    return ChannelEventRow(
        id=row_id,
        channel_id=channel_id,
        seq=seq,
        type="message.created",
        payload={},
        created_at=datetime.now(UTC),
    )


async def publish(outbox: FakeOutbox, producer: FakeProducer, log: list[str]) -> int:
    return await publish_batch(
        cast(AsyncSession, FakeSession(log)),
        cast(ChannelEventRepository, outbox),
        cast(AIOKafkaProducer, producer),
        "chat.events",
    )


async def test_sends_in_id_order_keyed_by_channel_then_marks_them():
    a, b = uuid.uuid4(), uuid.uuid4()
    outbox = FakeOutbox([row(1, a, 1), row(2, b, 1), row(3, a, 2)])
    producer, log = FakeProducer(), []

    assert await publish(outbox, producer, log) == 3

    assert [(key, event.seq) for _, key, event in producer.sent] == [(a, 1), (b, 1), (a, 2)]
    assert outbox.published == [1, 2, 3]
    assert log == ["commit", "commit"]  # the read, then the mark


async def test_a_kafka_error_marks_nothing():
    outbox, log = FakeOutbox([row(1, uuid.uuid4(), 1)]), []

    with pytest.raises(KafkaTimeoutError):
        await publish(outbox, FakeProducer(error=KafkaTimeoutError()), log)

    assert outbox.published == []
    assert log == ["commit"]  # only the read; no transaction is open while it sends


async def test_an_empty_outbox_sends_nothing():
    producer, log = FakeProducer(), []

    assert await publish(FakeOutbox([]), producer, log) == 0

    assert producer.sent == []
    assert log == ["commit"]  # the read only


async def test_sends_at_most_one_batch():
    channel = uuid.uuid4()
    outbox = FakeOutbox([row(i, channel, i) for i in range(1, BATCH_SIZE + 2)])

    assert await publish(outbox, FakeProducer(), []) == BATCH_SIZE
