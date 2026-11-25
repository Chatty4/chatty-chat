"""What an image shows, as words from a fixed list, for search."""

from collections.abc import Callable
from functools import cache
from io import BytesIO
from typing import cast

import open_clip
import torch
from PIL import Image

MODEL, PRETRAINED = "ViT-B-32", "laion2b_s34b_b79k"  # the Dockerfile downloads the same weights
# shortcut: a hand-made list, CLIP can only answer with these words; grow it from what users search
LABELS = (
    "screenshot",
    "document",
    "receipt",
    "invoice",
    "chart",
    "diagram",
    "code",
    "logo",
    "icon",
    "meme",
    "person",
    "group of people",
    "selfie",
    "baby",
    "dog",
    "cat",
    "car",
    "food",
    "drink",
    "beach",
    "mountain",
    "forest",
    "city",
    "building",
    "office",
    "computer",
    "phone",
    "sunset",
    "sky",
    "flower",
    "map",
    "drawing",
    "table",
    "whiteboard",
    "presentation slide",
)
TOP = 3  # at most this many labels per image
MIN_SCORE = 0.1  # scores of all labels add up to 1; under this, the label is a weak guess

Preprocess = Callable[[Image.Image], torch.Tensor]  # resizes, crops and normalizes for CLIP


@cache
def _model() -> tuple[open_clip.CLIP, Preprocess, torch.Tensor]:
    """The model, its image preprocessing, and one vector per label. Loads once (~5 s)."""
    created, _, transform = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
    model = cast(open_clip.CLIP, created)  # ViT-B-32 builds a CLIP model
    preprocess = cast(Preprocess, transform)  # an image Compose for image models
    model.eval()
    tokenizer = open_clip.get_tokenizer(MODEL)
    with torch.no_grad():
        labels = model.encode_text(tokenizer([f"a photo of {label}" for label in LABELS]))
        labels /= labels.norm(dim=-1, keepdim=True)
    return model, preprocess, labels


def image_labels(data: bytes) -> list[str]:
    """Up to TOP labels for the image, best first.
    Raises PIL.UnidentifiedImageError if the bytes are not an image."""
    model, preprocess, labels = _model()
    with Image.open(BytesIO(data)) as image:
        image.seek(0)  # first frame of an animated GIF
        pixels = preprocess(image.convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        vector = model.encode_image(pixels)
        vector /= vector.norm(dim=-1, keepdim=True)
        scores = (100 * vector @ labels.T).softmax(dim=-1)[0]
    best = scores.topk(TOP)
    return [
        LABELS[index]
        for score, index in zip(best.values.tolist(), best.indices.tolist(), strict=True)
        if score >= MIN_SCORE
    ]
