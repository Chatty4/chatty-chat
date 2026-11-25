from io import BytesIO

import pytest
from PIL import Image, UnidentifiedImageError

from app.labeling.colors import dominant_colors


def png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_solid_red():
    assert dominant_colors(png(Image.new("RGB", (100, 100), (220, 20, 30)))) == ["red"]


def test_two_colors_most_common_first():
    image = Image.new("RGB", (100, 100), (20, 40, 200))  # blue
    image.paste((220, 20, 30), (0, 0, 100, 30))  # red top 30%
    assert dominant_colors(png(image)) == ["blue", "red"]


def test_small_spot_is_ignored():
    image = Image.new("RGB", (100, 100), (255, 255, 255))
    image.paste((30, 160, 40), (0, 0, 10, 10))  # green 1%
    assert dominant_colors(png(image)) == ["white"]


def test_transparent_background_is_not_black():
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    image.paste((220, 20, 30, 255), (25, 25, 75, 75))
    assert dominant_colors(png(image)) == ["red"]


def test_not_an_image_raises():
    with pytest.raises(UnidentifiedImageError):
        dominant_colors(b"not an image")
