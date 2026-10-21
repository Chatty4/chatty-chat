import uuid

from sqlalchemy import BigInteger, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChannelCounter(Base):
    """One row per channel."""

    __tablename__ = "channel_counters"

    channel_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
