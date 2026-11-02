import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Pin(Base):
    """A pinned message. message_id is the key, so a message is pinned at most once."""

    __tablename__ = "pins"
    __table_args__ = (Index("ix_pins_channel_id_pinned_at", "channel_id", "pinned_at"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[uuid.UUID]
    pinned_by: Mapped[uuid.UUID]
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
