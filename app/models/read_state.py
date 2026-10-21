import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReadState(Base):
    __tablename__ = "read_state"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    last_read_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
