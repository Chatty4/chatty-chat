from io import BytesIO

import pytest
from PIL import Image, ImageDraw, UnidentifiedImageError

pytest.importorskip("open_clip")  # installed only in the labeler image

from app.labeling.clip import TOP, image_labels  # noqa: E402


def code_screenshot() -> bytes:
    image = Image.new("RGB", (224, 224), "white")
    draw = ImageDraw.Draw(image)
    for y in range(20, 200, 20):
        draw.text((10, y), "def main(): return 42  # code", fill="black")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_code_is_labeled_code():
    assert image_labels(code_screenshot())[0] == "code"


def test_at_most_top_labels():
    assert len(image_labels(code_screenshot())) <= TOP


def test_not_an_image_raises():
    with pytest.raises(UnidentifiedImageError):
        image_labels(b"not an image")
