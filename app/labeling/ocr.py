"""Text written inside an image, for search."""

from functools import cache

from rapidocr import RapidOCR
from rapidocr.utils.output import RapidOCROutput


@cache
def _engine() -> RapidOCR:
    return RapidOCR()  # loads 3 models (~30 MB, ~2 s): once per process


def image_text(data: bytes) -> str:
    """Lines of text found in the image, top to bottom. "" if there is none.
    Raises PIL.UnidentifiedImageError if the bytes are not an image."""
    result = _engine()(data)  # drops lines scoring under 0.5 (RapidOCR's default text_score)
    assert isinstance(result, RapidOCROutput)  # detect + classify + recognize are all on by default
    if result.txts is None:
        return ""
    return "\n".join(text.strip() for text in result.txts)
