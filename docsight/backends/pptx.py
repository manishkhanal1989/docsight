"""PowerPoint OOXML (.pptx) extraction, standard library only.

Slides are the format most likely to *be* a pile of scans: people paste
document photos onto blank slides, one per page, and send the deck. So
images here matter more than text, and each is tagged with its slide.

Presenter notes are captured but marked ``notes`` -- useful context, never
the document itself.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

from ..types import DocKind, Document, TextBlock
from .opc import OpcPackage

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

MARKER = "ppt/presentation.xml"
MEDIA = "ppt/media/"

_SLIDE_NO = re.compile(r"slide(\d+)\.xml$")


def extract(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse a .pptx into per-slide text and image blocks.

    *max_pages* caps the number of slides read.
    """
    path = Path(path)
    doc = Document(path=path, kind=DocKind.PPTX)

    with OpcPackage(path) as pkg:
        doc.meta.update(pkg.core_properties())
        slides = pkg.matching("ppt/slides/", ".xml")
        if max_pages is not None:
            slides = slides[:max_pages]
        doc.meta["slide_count"] = len(slides)

        for part in slides:
            match = _SLIDE_NO.search(part)
            number = int(match.group(1)) if match else None
            section = f"slide{number}" if number else Path(part).stem

            root = pkg.xml(part)
            if root is None:
                continue

            text = _shape_text(root)
            if text.strip():
                doc.blocks.append(
                    TextBlock(text, "slide", next(pkg.order), page=number, section=section)
                )

            if embedded_images:
                for img in pkg.images_in(root, part, section=section):
                    doc.blocks.append(
                        replace(img, order=next(pkg.order), page=number)
                    )

            notes = _notes_for(pkg, part)
            if notes.strip():
                doc.blocks.append(
                    TextBlock(notes, "notes", next(pkg.order), page=number, section=section)
                )

        if embedded_images:
            doc.blocks.extend(pkg.orphan_media(MEDIA))
        pkg.note_embeddings("ppt")
        doc.warnings.extend(pkg.warnings)

    return doc


def _shape_text(root: ET.Element) -> str:
    """Text of every shape on a slide, one paragraph per line.

    Grouping by ``a:p`` keeps a title and its bullets on separate lines,
    which a naive join of all ``a:t`` runs would merge into one blob.
    """
    lines: list[str] = []
    for para in root.iter(f"{A}p"):
        runs = "".join(t.text or "" for t in para.iter(f"{A}t"))
        if runs.strip():
            lines.append(runs.strip())
    return "\n".join(lines)


def _notes_for(pkg: OpcPackage, slide_part: str) -> str:
    """Presenter notes attached to one slide, if any."""
    out: list[str] = []
    for notes_part in pkg.related_parts(slide_part, "/notesSlide"):
        root = pkg.xml(notes_part)
        if root is not None:
            out.append(_shape_text(root))
    return "\n".join(t for t in out if t.strip())
