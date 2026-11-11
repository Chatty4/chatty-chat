import logging
import uuid

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.events import ChannelEvent

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_MS = 10_000


def encode_key(channel_id: uuid.UUID | None) -> bytes:
    """Raises without a key: aiokafka would send b"None" and lose the order per channel."""
    if channel_id is None:
        raise ValueError("Send channel events with key=event.channel_id")
    return str(channel_id).encode()


def decode_event(value: bytes) -> ChannelEvent | None:
    """None for a bad record, so it can't block its partition."""
    try:
        return ChannelEvent.model_validate_json(value)
    except ValidationError:
        logger.warning("Skipping a record that isn't a channel event: %.200r", value)
        return None


def create_producer() -> AIOKafkaProducer:
    """Sends ChannelEvents keyed by channel_id. The caller starts and stops it."""
    return AIOKafkaProducer(
        bootstrap_servers=get_settings().KAFKA_BOOTSTRAP_SERVERS,
        acks="all",
        enable_idempotence=True,
        request_timeout_ms=REQUEST_TIMEOUT_MS,
        key_serializer=encode_key,
        value_serializer=lambda event: event.model_dump_json().encode(),
    )


def create_consumer(group_id: str, *topics: str) -> AIOKafkaConsumer:
    """Values are ChannelEvents, or None for a bad record. The caller commits after each one."""
    return AIOKafkaConsumer(
        *(topics or (get_settings().KAFKA_TOPIC,)),
        bootstrap_servers=get_settings().KAFKA_BOOTSTRAP_SERVERS,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        request_timeout_ms=REQUEST_TIMEOUT_MS,
        value_deserializer=decode_event,
    )
