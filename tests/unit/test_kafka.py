import uuid
from datetime import UTC, datetime

import pytest

from app.clients.kafka import decode_event, encode_key
from app.schemas.events import ChannelEvent, EventType


def test_encode_key_is_the_channel_id_as_text():
    channel_id = uuid.uuid4()

    assert encode_key(channel_id) == str(channel_id).encode()


def test_encode_key_refuses_a_missing_key():
    # aiokafka calls the serializer with None when send() gets no key
    with pytest.raises(ValueError, match="key=event.channel_id"):
        encode_key(None)


def test_decode_event_reads_a_channel_event():
    event = ChannelEvent(
        type=EventType.MESSAGE_CREATED,
        channel_id=uuid.uuid4(),
        seq=1,
        created_at=datetime.now(UTC),
        payload={"message": {"body": "hi"}},
    )

    assert decode_event(event.model_dump_json().encode()) == event


@pytest.mark.parametrize(
    "value",
    [b"hello", b"{}", b'{"type": "message.created"}', b"[]"],
    ids=["not json", "empty object", "missing fields", "not an object"],
)
def test_decode_event_gives_none_for_anything_else(value):
    assert decode_event(value) is None
