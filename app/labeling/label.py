"""Search words and text of one attachment, from its bytes."""

from app.labeling.colors import dominant_colors
from app.labeling.documents import READERS, document_text

IMAGES = {"image/png", "image/jpeg", "image/webp", "image/gif"}  # api-chat.md


def is_labeled(mime: str) -> bool:
    """Other types match by file name only, so they're never downloaded."""
    return mime in IMAGES or mime in READERS or mime.startswith("text/")


def labels_and_text(data: bytes, mime: str) -> tuple[str | None, str | None]:
    """(labels, content) for the attachment's tsv. Raises the library's error on a broken file."""
    if mime in IMAGES:
        # imported here: torch and RapidOCR exist only in the labeler image
        from app.labeling.clip import image_labels
        from app.labeling.ocr import image_text

        words = dominant_colors(data) + image_labels(data)
        return ", ".join(words) or None, image_text(data) or None
    return None, document_text(data, mime) or None
