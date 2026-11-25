import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SEARCH_CONFIG = "english"  # the search query uses it too; changing it needs a migration

# filename A, labels B, content C; the filename is split on ._- so "report_q3.pdf" matches "report"
ATTACHMENT_TSV = (
    f"setweight(to_tsvector('{SEARCH_CONFIG}', regexp_replace(filename, '[._-]+', ' ', 'g')), 'A')"
    f" || setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(labels, '')), 'B')"
    f" || setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(content, '')), 'C')"
)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "seq"),
        UniqueConstraint("author_id", "client_msg_id"),  # retries return the original message
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_messages_reply_to_id",
            "reply_to_id",
            postgresql_where=text("reply_to_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID]
    author_id: Mapped[uuid.UUID]
    seq: Mapped[int] = mapped_column(BigInteger)
    client_msg_id: Mapped[uuid.UUID]
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    body: Mapped[str] = mapped_column(Text)
    mentions: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=text("'{}'"))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            f"to_tsvector('{SEARCH_CONFIG}', regexp_replace(body, '<@[0-9a-f-]+>', ' ', 'g'))",
            persisted=True,
        ),
        deferred=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attachment(Base):
    """What chatty-core's attach returned for a file (D-03)."""

    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_tsv", "tsv", postgresql_using="gin"),
        Index("ix_attachments_unlabeled", "file_id", postgresql_where=text("labeled_at IS NULL")),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)  # the file's index in the sent file_ids
    object_key: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    labels: Mapped[str | None] = mapped_column(Text)  # colors + CLIP labels
    content: Mapped[str | None] = mapped_column(Text, deferred=True)  # OCR or document text, capped
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed(ATTACHMENT_TSV, persisted=True), deferred=True
    )
