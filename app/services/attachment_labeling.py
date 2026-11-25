import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.storage import read_attachment
from app.labeling.label import is_labeled, labels_and_text
from app.repositories import AttachmentRepository

logger = logging.getLogger("app.services.attachment_labeling")


def _download_and_label(object_key: str, mime: str) -> tuple[str | None, str | None]:
    return labels_and_text(read_attachment(object_key), mime)


async def label_next(session: AsyncSession, attachments: AttachmentRepository) -> bool:
    """Labels one attachment. False when none is left. A failure is logged and leaves it
    unlabeled, with the attempt already counted (claim_unlabeled)."""
    async with session.begin():
        attachment = await attachments.claim_unlabeled()
        if attachment is None:
            return False
        file_id, object_key, mime = attachment.file_id, attachment.object_key, attachment.mime

    labels = content = None
    if is_labeled(mime):
        try:
            # no transaction is open here: OCR and CLIP take seconds
            labels, content = await asyncio.to_thread(_download_and_label, object_key, mime)
        except Exception:
            logger.warning("Labeling failed: file %s (%s)", file_id, mime, exc_info=True)
            return True

    async with session.begin():
        await attachments.save_labels(file_id, labels, content)
    return True
