"""Turning extracted blocks into a vision-model request payload.

Covers the three things that go wrong between extraction and the API call:
a format the API rejects, an image so large it blows the token budget, and
text that should have been sent alongside the pixels as a cross-check.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Literal

from .imagesize import probe
from .routing import VECTOR_MIMES
from .types import ImageBlock

Dialect = Literal["openai", "anthropic"]

#: Media types vision APIs accept directly. Everything else needs
#: converting, and vector metafiles need rendering instead.
API_SAFE = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

#: Long-edge cap. Qwen-VL and friends tile large images into many patches;
#: past ~1600px a certificate scan costs materially more tokens without
#: reading any better.
DEFAULT_MAX_EDGE = 1600


def data_url(img: ImageBlock) -> str:
    """``data:`` URL for an image, as OpenAI-compatible endpoints expect."""
    return f"data:{img.mime};base64,{base64.b64encode(img.data).decode('ascii')}"


def normalize(
    img: ImageBlock,
    *,
    max_edge: int | None = DEFAULT_MAX_EDGE,
    jpeg_quality: int = 88,
) -> ImageBlock | None:
    """Re-encode an image into something the API accepts, and shrink it.

    Returns None when the image cannot be made usable -- EMF/WMF metafiles,
    which must be reached through :func:`docsight.render.render_pages`
    instead. Returns the input unchanged when no work is needed, so the
    original scan bytes survive untouched in the common case.
    """
    if img.mime in VECTOR_MIMES:
        return None

    # Dimensions may be absent on a hand-built block; probe rather than
    # assume small, or an oversized scan would sail past the edge cap.
    width, height = img.width, img.height
    if (width is None or height is None) and max_edge:
        size = probe(img.data)
        if size:
            width, height = size

    needs_convert = img.mime not in API_SAFE
    too_big = bool(max_edge and width and height and max(width, height) > max_edge)
    if not needs_convert and not too_big:
        return img if img.width else replace(img, width=width, height=height)

    try:
        from PIL import Image
    except ImportError:
        # Without Pillow, pass an API-safe image through rather than fail;
        # an unsupported one cannot be rescued.
        return img if not needs_convert else None

    try:
        with Image.open(io.BytesIO(img.data)) as im:
            im = im.convert("RGB")
            if max_edge and max(im.size) > max_edge:
                scale = max_edge / max(im.size)
                im = im.resize(
                    (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                    Image.LANCZOS,
                )
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            return ImageBlock(
                data=buf.getvalue(),
                mime="image/jpeg",
                name=img.name,
                order=img.order,
                page=img.page,
                width=im.width,
                height=im.height,
                source="rendered",
                referenced=img.referenced,
            )
    except Exception:
        return img if not needs_convert else None


def image_part(img: ImageBlock, dialect: Dialect = "openai") -> dict[str, Any]:
    """One image content part in the requested API dialect."""
    if dialect == "anthropic":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.mime,
                "data": base64.b64encode(img.data).decode("ascii"),
            },
        }
    return {"type": "image_url", "image_url": {"url": data_url(img)}}


def text_part(text: str, dialect: Dialect = "openai") -> dict[str, Any]:
    return {"type": "text", "text": text}


def build_content(
    images: Iterable[ImageBlock],
    *,
    prompt: str,
    text_layer: str | None = None,
    dialect: Dialect = "openai",
    max_edge: int | None = DEFAULT_MAX_EDGE,
) -> tuple[list[dict[str, Any]], list[ImageBlock]]:
    """Assemble a message ``content`` list.

    Returns ``(content, unusable)`` -- *unusable* holds images that could
    not be converted, so the caller can fall back to page rendering instead
    of silently verifying a document it never actually saw.
    """
    content: list[dict[str, Any]] = [text_part(prompt, dialect)]
    unusable: list[ImageBlock] = []

    for img in images:
        ready = normalize(img, max_edge=max_edge)
        if ready is None:
            unusable.append(img)
            continue
        content.append(image_part(ready, dialect))

    if text_layer and text_layer.strip():
        content.append(
            text_part(
                "Text layer extracted from the source file, for cross-checking "
                "what you read in the image(s):\n\n" + text_layer.strip(),
                dialect,
            )
        )
    return content, unusable
