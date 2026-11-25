from io import BytesIO

import pytest
from PIL import Image

from app.labeling.label import is_labeled, labels_and_text


def test_only_known_types_are_labeled():
    assert is_labeled("image/png") and is_labeled("application/pdf") and is_labeled("text/csv")
    assert not is_labeled("video/mp4") and not is_labeled("application/zip")


def test_text_goes_to_content():
    assert labels_and_text(b"quarterly revenue", "text/plain") == (None, "quarterly revenue")


def test_empty_text_is_none():
    assert labels_and_text(b"", "text/plain") == (None, None)


def test_image_has_colors_and_labels():
    pytest.importorskip("open_clip")  # labeler image only
    buffer = BytesIO()
    Image.new("RGB", (224, 224), (220, 20, 20)).save(buffer, "PNG")

    labels, content = labels_and_text(buffer.getvalue(), "image/png")

    assert labels is not None and labels.startswith("red, ")  # colors first, then CLIP's words
    assert content is None  # no text in a plain red square
