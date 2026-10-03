"""Deciding how a document should reach the vision model.

The expensive mistake in a verification pipeline is sending every document
through the vision model. A Word file with a real text layer can be parsed
exactly and for free; only scans need pixels. This module makes that call.
"""

from __future__ import annotations

from .filters import ImageFilter, apply
from .types import Document, Strategy

#: Below this many body characters a document cannot carry the fields a
#: record needs -- names, a date, a place, an issuing authority, a reference
#: number. Anything shorter is a caption on a scan.
MIN_TEXT_CHARS = 180

#: Per-family overrides. A slide deck of photographed pages carries titles
#: and page furniture that would clear the prose threshold without holding
#: any of the record, so it has to be read as pixels. Spreadsheets go the
#: other way: a handful of labelled cells is a complete, exact answer.
FAMILY_THRESHOLDS: dict[str, int] = {
    "slides": 400,
    "sheet": 80,
    "markup": 120,
}


def threshold_for(doc: Document, default: int = MIN_TEXT_CHARS) -> int:
    """The text threshold appropriate to this document's family."""
    if default != MIN_TEXT_CHARS:
        return default  # an explicit caller value always wins
    return FAMILY_THRESHOLDS.get(doc.kind.family, default)


def decide(
    doc: Document,
    *,
    min_text_chars: int = MIN_TEXT_CHARS,
    image_filter: ImageFilter | None = None,
    union: bool = False,
) -> Strategy:
    """Classify *doc* into a handling strategy.

    Only images that survive filtering count toward HYBRID, so a text
    document decorated with an agency logo is not mistaken for a scan.

    EMPTY is reserved for a file with no text and no images whatsoever.
    Text that merely falls *below* the trust threshold is not emptiness --
    it is a document that needs looking at, so it reports IMAGE_ONLY and
    ``prepare()`` renders the page. Conflating the two would make the
    pipeline reject uploads that plainly contain a record.

    *union* folds in descendants, for judging a container as a whole.
    """
    text = doc.all_body_text if union else doc.body_text
    raw_images = doc.all_images if union else doc.images
    kept, _ = apply(raw_images, image_filter or ImageFilter())

    has_text = len(text) >= threshold_for(doc, min_text_chars)
    has_images = bool(kept)

    if has_text and has_images:
        return Strategy.HYBRID
    if has_text:
        return Strategy.TEXT_ONLY
    if has_images:
        return Strategy.IMAGE_ONLY
    if not text.strip() and not raw_images:
        return Strategy.EMPTY
    if not raw_images and not doc.kind.can_hold_images:
        # A short .txt or .csv is simply a short document. Routing it to
        # vision would ask for pixels that cannot exist and leave the caller
        # holding an "unverifiable" verdict on a perfectly readable file.
        return Strategy.TEXT_ONLY
    # Weak text, or images that all filtered out: look at the pixels.
    return Strategy.IMAGE_ONLY


def explain(
    doc: Document,
    *,
    min_text_chars: int = MIN_TEXT_CHARS,
    image_filter: ImageFilter | None = None,
    union: bool = False,
) -> str:
    """One line of reasoning for the chosen strategy, for logs and audit."""
    flt = image_filter or ImageFilter()
    raw = doc.all_images if union else doc.images
    text = doc.all_body_text if union else doc.body_text
    kept, rejected = apply(raw, flt)
    strategy = decide(
        doc, min_text_chars=min_text_chars, image_filter=flt, union=union
    )
    return (
        f"{strategy.value}: {len(text)} body chars "
        f"(threshold {threshold_for(doc, min_text_chars)}), "
        f"{len(kept)} image(s) kept, {len(rejected)} filtered out"
    )
