"""Bare image files -- the common case when a dependent photographs a
certificate with a phone.

Multi-page TIFFs are split into one block per page, and HEIC is registered
into Pillow when ``pillow-heif`` is installed. iPhone uploads are HEIC far
more often than people expect.
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path

from ..imagesize import probe
from ..routing import sniff_mime
from ..types import DocKind, Document, ImageBlock

_heif_registered = False


def _register_heif() -> bool:
    """Teach Pillow about HEIC/HEIF; returns whether support is available."""
    global _heif_registered
    if _heif_registered:
        return True
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        return False
    _heif_registered = True
    return True


def extract(path: str | Path) -> Document:
    """Wrap an image file as a single-image document."""
    path = Path(path)
    data = path.read_bytes()
    mime = sniff_mime(data, path.name)
    doc = Document(path=path, kind=DocKind.IMAGE)
    order = itertools.count()

    if mime == "image/heic" and not _register_heif():
        doc.warnings.append(
            "HEIC input and pillow-heif is not installed; the bytes are passed "
            "through unconverted and most vision APIs will reject them. "
            "Install with: pip install pillow-heif"
        )

    if mime == "image/tiff":
        pages = _split_tiff(data, order, doc.warnings)
        if pages:
            doc.blocks.extend(pages)
            doc.meta["page_count"] = len(pages)
            return doc

    size = probe(data)
    doc.blocks.append(
        ImageBlock(
            data=data,
            mime=mime,
            name=path.name,
            order=next(order),
            page=1,
            width=size[0] if size else None,
            height=size[1] if size else None,
            source="embedded",
        )
    )
    return doc


def _split_tiff(data: bytes, order, warnings: list[str]) -> list[ImageBlock]:
    """Explode a multi-page TIFF into per-page PNGs.

    Faxed and scanned certificates arrive as multi-page TIFF routinely, and
    a vision API handed the raw container sees only the first page.
    """
    try:
        from PIL import Image
    except ImportError:
        warnings.append(
            "TIFF input and Pillow is not installed; multi-page files will be "
            "truncated to the first page by downstream consumers. "
            "Install with: pip install pillow"
        )
        return []

    out: list[ImageBlock] = []
    try:
        with Image.open(io.BytesIO(data)) as im:
            frames = getattr(im, "n_frames", 1)
            for i in range(frames):
                im.seek(i)
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="PNG")
                png = buf.getvalue()
                out.append(
                    ImageBlock(
                        data=png,
                        mime="image/png",
                        name=f"page{i + 1}.png",
                        order=next(order),
                        page=i + 1,
                        width=im.width,
                        height=im.height,
                        source="rendered",
                    )
                )
    except Exception as exc:
        warnings.append(f"TIFF pages could not be split ({exc})")
        return []
    return out
