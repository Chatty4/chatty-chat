from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

pytest.importorskip("rapidocr")  # installed only in the labeler image

from app.labeling.ocr import image_text  # noqa: E402


def png(text: str | None, mode: str = "RGB", background=(255, 255, 255)) -> bytes:
    image = Image.new(mode, (600, 100), background)
    if text:
        font = ImageFont.load_default(size=36)
        ImageDraw.Draw(image).text((20, 20), text, fill="black", font=font)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_reads_text():
    assert image_text(png("Invoice 2231")) == "Invoice 2231"


def test_no_text_is_empty():
    assert image_text(png(None)) == ""


def test_text_on_transparent_background():
    assert image_text(png("Bucharest", "RGBA", (0, 0, 0, 0))) == "Bucharest"


def test_not_an_image_raises():
    with pytest.raises(UnidentifiedImageError):
        image_text(b"not an image")
