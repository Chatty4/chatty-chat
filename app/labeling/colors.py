"""Dominant color names of an image, for search."""

import colorsys
from io import BytesIO
from typing import cast

from PIL import Image

MIN_SHARE = 0.10  # a color must cover this much of the visible image to count
SIZE = (64, 64)  # enough pixels to judge colors, fast to count
ColorCounts = list[tuple[int, tuple[int, int, int, int]]]  # (count, RGBA pixel)


def dominant_colors(data: bytes) -> list[str]:
    """Color names covering at least MIN_SHARE of the image, most common first.
    Raises PIL.UnidentifiedImageError if the bytes are not an image."""
    with Image.open(BytesIO(data)) as image:
        image.seek(0)  # first frame of an animated GIF
        small = image.convert("RGBA")
    small.thumbnail(SIZE)

    counts: dict[str, int] = {}
    visible = 0
    colors = cast(ColorCounts, small.getcolors(SIZE[0] * SIZE[1]))
    for count, (r, g, b, a) in colors:
        if a < 128:  # transparent pixels would otherwise count as black
            continue
        name = color_name(r, g, b)
        counts[name] = counts.get(name, 0) + count
        visible += count

    shares = {name: count / visible for name, count in counts.items()} if visible else {}
    return [
        name for name, share in sorted(shares.items(), key=lambda x: -x[1]) if share >= MIN_SHARE
    ]


def color_name(r: int, g: int, b: int) -> str:
    # shortcut: 11 names by hue ranges, add more (teal, beige, ...) if users search for them
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < 0.2:
        return "black"
    if s < 0.15:
        return "white" if v > 0.85 else "gray"
    hue = h * 360
    if hue < 15 or hue >= 345:
        return "red"
    if hue < 40:
        return "brown" if v < 0.6 else "orange"
    if hue < 70:
        return "yellow"
    if hue < 170:
        return "green"
    if hue < 260:
        return "blue"
    if hue < 290:
        return "purple"
    return "pink"
