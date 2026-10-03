"""Dropping images that are not worth a vision call.

Certificates arrive surrounded by noise: state seals, agency logos,
signature scribbles, header rules, tracking barcodes. Each one sent to a
vision model costs tokens and latency and returns nothing. Filtering on
size catches nearly all of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .routing import VECTOR_MIMES
from .types import ImageBlock


@dataclass(frozen=True)
class ImageFilter:
    """Thresholds for keeping an image.

    Defaults are tuned for scanned identity and vital records: a legible
    certificate scan is at least a few hundred pixels on its short side and
    comfortably over 15 KB, while seals and logos fall well below both.
    """

    min_width: int = 200
    min_height: int = 200
    min_bytes: int = 15_000
    min_pixels: int = 90_000  # ~300x300
    max_images: int | None = None
    drop_duplicates: bool = True
    drop_vector: bool = False
    #: Keep images whose dimensions could not be determined. Safer than
    #: dropping a real certificate in an unrecognised wrapper.
    keep_unsized: bool = True

    def accepts(self, img: ImageBlock) -> tuple[bool, str]:
        """Return ``(keep, reason)`` -- reason explains a rejection."""
        if self.drop_vector and img.mime in VECTOR_MIMES:
            return False, f"vector format {img.mime}"
        if img.nbytes < self.min_bytes:
            return False, f"{img.nbytes}B under min_bytes={self.min_bytes}"
        if img.width is None or img.height is None:
            return bool(self.keep_unsized), "dimensions unknown"
        if img.width < self.min_width or img.height < self.min_height:
            return False, f"{img.width}x{img.height} under minimum"
        if img.width * img.height < self.min_pixels:
            return False, f"{img.width * img.height}px under min_pixels"
        return True, "ok"


#: Keep everything -- for auditing what a document actually contained.
KEEP_ALL = ImageFilter(
    min_width=0, min_height=0, min_bytes=0, min_pixels=0, drop_duplicates=False
)

#: Only large, high-quality scans. Useful when cost per document matters
#: more than recall.
STRICT = ImageFilter(min_width=600, min_height=600, min_bytes=60_000, min_pixels=500_000)


def apply(
    images: list[ImageBlock], flt: ImageFilter | None = None
) -> tuple[list[ImageBlock], list[tuple[ImageBlock, str]]]:
    """Partition *images* into kept and ``(image, reason)`` rejected.

    Largest first among the kept, so the page most likely to be the
    certificate is the first thing the model sees.
    """
    flt = flt or ImageFilter()
    kept: list[ImageBlock] = []
    rejected: list[tuple[ImageBlock, str]] = []
    seen: set[str] = set()

    for img in images:
        ok, reason = flt.accepts(img)
        if not ok:
            rejected.append((img, reason))
            continue
        if flt.drop_duplicates:
            digest = img.sha256
            if digest in seen:
                rejected.append((img, "duplicate of an earlier image"))
                continue
            seen.add(digest)
        kept.append(img)

    kept.sort(key=lambda i: (-(i.pixels or 0), -i.nbytes, i.order))
    if flt.max_images is not None and len(kept) > flt.max_images:
        for img in kept[flt.max_images :]:
            rejected.append((img, f"beyond max_images={flt.max_images}"))
        kept = kept[: flt.max_images]
    return kept, rejected
