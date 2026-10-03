"""Rasterising a document as it *looks*.

Two reasons a pipeline needs this even when embedded images extract
cleanly:

* The document may be typed text with no image at all, yet the layout still
  carries meaning (which name sits in the "spouse" column).
* Word stores pasted clipboard graphics as EMF/WMF metafiles that no vision
  API accepts. Rendering is the only way to see them.
"""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path

from .backends import office
from .backends.pdf import _pymupdf
from .types import DocKind, ImageBlock

#: 200-300 DPI is the usable band for OCR. Below ~150 small print degrades;
#: above ~400 the payload grows with no accuracy gain.
DEFAULT_DPI = 250

#: Everything LibreOffice can lay out and convert to PDF.
CONVERTIBLE = frozenset(
    {
        DocKind.DOCX, DocKind.DOC, DocKind.RTF,
        DocKind.XLSX, DocKind.XLS,
        DocKind.PPTX, DocKind.PPT,
        DocKind.ODT, DocKind.ODS, DocKind.ODP,
        DocKind.HTML,
    }
)


def render_pages(
    path: str | Path,
    kind: DocKind | None = None,
    *,
    dpi: int = DEFAULT_DPI,
    max_pages: int | None = None,
) -> list[ImageBlock]:
    """Return one PNG ``ImageBlock`` per page of the document at *path*."""
    from .routing import detect

    path = Path(path)
    kind = kind or detect(path)

    if kind == DocKind.IMAGE:
        from .backends.image import extract as image_extract

        return image_extract(path).images

    if kind == DocKind.PDF:
        return _render_pdf(path, dpi=dpi, max_pages=max_pages)

    if kind in CONVERTIBLE:
        pdf = office.to_pdf(path)
        try:
            return _render_pdf(pdf, dpi=dpi, max_pages=max_pages)
        finally:
            shutil.rmtree(pdf.parent, ignore_errors=True)

    raise ValueError(
        f"cannot render {kind.value if kind else 'unknown'} documents"
        + (
            "; extract its members and render those instead"
            if kind and kind.is_container
            else ""
        )
    )


def _render_pdf(path: Path, *, dpi: int, max_pages: int | None) -> list[ImageBlock]:
    fitz = _pymupdf()
    order = itertools.count()
    out: list[ImageBlock] = []

    with fitz.open(path) as pdf:
        limit = pdf.page_count if max_pages is None else min(max_pages, pdf.page_count)
        for pno in range(limit):
            pix = pdf.load_page(pno).get_pixmap(dpi=dpi)
            out.append(
                ImageBlock(
                    data=pix.tobytes("png"),
                    mime="image/png",
                    name=f"page{pno + 1}.png",
                    order=next(order),
                    page=pno + 1,
                    width=pix.width,
                    height=pix.height,
                    source="rendered",
                )
            )
    return out


def can_render(kind: DocKind) -> bool:
    """Whether rendering this kind is possible in the current environment."""
    if kind in CONVERTIBLE:
        return office.available()
    return kind in (DocKind.PDF, DocKind.IMAGE)
