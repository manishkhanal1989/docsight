"""PDF extraction via PyMuPDF.

PyMuPDF covers both halves of the job: pulling the text layer (so a
born-digital PDF can skip the vision call) and pulling embedded image
streams at original resolution.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from ..errors import CorruptDocument, MissingDependency
from ..types import DocKind, Document, ImageBlock, TextBlock

_HINT = "Install it with: pip install pymupdf"


def _pymupdf():
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # PyMuPDF < 1.24 exposes itself as fitz

        return fitz
    except ImportError as exc:
        raise MissingDependency("PyMuPDF", _HINT) from exc


def extract(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse a PDF into per-page text blocks and embedded image blocks."""
    fitz = _pymupdf()
    path = Path(path)
    doc = Document(path=path, kind=DocKind.PDF)
    order = itertools.count()

    try:
        pdf = fitz.open(path)
    except Exception as exc:  # pymupdf raises a wide range here
        raise CorruptDocument(f"{path.name} could not be opened as PDF: {exc}") from exc

    with pdf:
        if pdf.needs_pass:
            raise CorruptDocument(f"{path.name} is password-protected")

        doc.meta.update({k: v for k, v in (pdf.metadata or {}).items() if v})
        doc.meta["page_count"] = pdf.page_count

        limit = pdf.page_count if max_pages is None else min(max_pages, pdf.page_count)
        for pno in range(limit):
            page = pdf.load_page(pno)

            text = page.get_text("text").strip()
            if text:
                doc.blocks.append(TextBlock(text, "page", next(order), page=pno + 1))

            if embedded_images:
                doc.blocks.extend(_page_images(fitz, pdf, page, pno, order, doc.warnings))

    return doc


def _page_images(fitz, pdf, page, pno: int, order, warnings: list[str]) -> list[ImageBlock]:
    """Extract image XObjects referenced by one page, at native resolution."""
    out: list[ImageBlock] = []
    seen: set[int] = set()

    for info in page.get_images(full=True):
        xref = info[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            raw = pdf.extract_image(xref)
        except Exception as exc:
            warnings.append(f"page {pno + 1}: image xref {xref} unreadable ({exc})")
            continue

        ext = raw.get("ext", "bin")
        out.append(
            ImageBlock(
                data=raw["image"],
                mime=f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}",
                name=f"p{pno + 1}-x{xref}.{ext}",
                order=next(order),
                page=pno + 1,
                width=raw.get("width"),
                height=raw.get("height"),
                source="embedded",
            )
        )
    return out
