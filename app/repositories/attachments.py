import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Message

MAX_ATTEMPTS = 3


class AttachmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_unlabeled(self) -> Attachment | None:
        """The next attachment to label, with this attempt already counted: commit before
        labeling, so a file that crashes the worker is tried at most MAX_ATTEMPTS times."""
        # one labeler runs at a time (advisory lock in app/jobs/label_attachments.py), so a file
        # claimed and committed here isn't claimed again by another worker
        next_file = (
            select(Attachment.file_id)
            .join(Message, Message.id == Attachment.message_id)
            .where(
                Attachment.labeled_at.is_(None),
                Attachment.label_attempts < MAX_ATTEMPTS,
                Message.deleted_at.is_(None),
            )  # chatty-core deletes their files; never searched
            .order_by(Attachment.label_attempts)  # new files first, retries after them
            .limit(1)
            .with_for_update(of=Attachment, skip_locked=True)  # never lock the message
            .scalar_subquery()
        )
        stmt = (
            update(Attachment)
            .where(Attachment.file_id == next_file)
            .values(label_attempts=Attachment.label_attempts + 1)
            .returning(Attachment)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def save_labels(
        self, file_id: uuid.UUID, labels: str | None, content: str | None
    ) -> None:
        """Done: the tsv column recomputes itself from labels and content."""
        stmt = (
            update(Attachment)
            .where(Attachment.file_id == file_id)
            .values(labels=labels, content=content, labeled_at=func.now())
        )
        await self._session.execute(stmt)
