"""Word OOXML (.docx) extraction, standard library only.

Walking the XML directly -- rather than leaning on the ``inline_shapes``
helper python-docx offers -- is deliberate. That helper only sees
``wp:inline`` drawings, and a certificate pasted into Word is very often a
*floating* (``wp:anchor``) image or a legacy VML shape. Those are precisely
the ones a verification pipeline must not drop.

The OPC plumbing it shares with .xlsx and .pptx lives in :mod:`.opc`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

from ..types import DocKind, Document, ImageBlock, Table, TextBlock, TextRole
from .opc import IMAGE_REFS, A, OpcPackage, V

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_HEADER_RE = re.compile(r"^word/header\d*\.xml$")
_FOOTER_RE = re.compile(r"^word/footer\d*\.xml$")

MARKER = "word/document.xml"
MEDIA = "word/media/"


def extract(path: str | Path, *, include_headers: bool = True) -> Document:
    """Parse a .docx into interleaved text and image blocks."""
    path = Path(path)
    doc = Document(path=path, kind=DocKind.DOCX)

    with OpcPackage(path) as pkg:
        doc.meta.update(pkg.core_properties())

        parts: list[tuple[str, TextRole]] = [(MARKER, "paragraph")]
        if include_headers:
            parts += [(n, "header") for n in sorted(pkg.names) if _HEADER_RE.match(n)]
            parts += [(n, "footer") for n in sorted(pkg.names) if _FOOTER_RE.match(n)]

        for part, role in parts:
            root = pkg.xml(part)
            if root is None:
                continue
            walker = _Walker(pkg, part, role)
            body = root.find(f"{W}body") if root.tag == f"{W}document" else root
            doc.blocks.extend(walker.walk(body if body is not None else root))
            doc.tables.extend(walker.tables)

        doc.blocks.extend(pkg.orphan_media(MEDIA))
        pkg.note_embeddings("word")
        doc.warnings.extend(pkg.warnings)
        doc.meta["media_count"] = sum(1 for n in pkg.names if n.startswith(MEDIA))

    return doc


class _Walker:
    """Emits blocks in document order for one Word part."""

    def __init__(self, pkg: OpcPackage, part: str, role: TextRole) -> None:
        self._pkg = pkg
        self._part = part
        self._role = role
        self.tables: list[Table] = []

    def walk(self, container: ET.Element):
        for child in container:
            if child.tag == f"{W}p":
                yield from self._paragraph(child, self._role)
            elif child.tag == f"{W}tbl":
                yield from self._table(child)
            elif child.tag == f"{W}sdt":  # content control: recurse into its body
                content = child.find(f"{W}sdtContent")
                if content is not None:
                    yield from self.walk(content)
            elif child.tag == f"{W}altChunk":
                self._pkg.warnings.append(
                    "contains an altChunk (embedded sub-document); that content "
                    "is not present in document.xml"
                )

    def _paragraph(self, p: ET.Element, role: TextRole):
        """Flush text and images in true encounter order.

        A paragraph holding both a caption and a scan must not report them
        out of order -- relative position is evidence in an audit trail.

        Tracked deletions are skipped for free: deleted runs carry their text
        in ``w:delText``, which this loop never reads.
        """
        buf: list[str] = []
        order = self._pkg.order

        def flush():
            text = "".join(buf).strip()
            buf.clear()
            if text:
                yield TextBlock(text, role, next(order))

        for node in p.iter():
            tag = node.tag
            if tag == f"{W}t":
                buf.append(node.text or "")
            elif tag == f"{W}tab":
                buf.append("\t")
            elif tag in (f"{W}br", f"{W}cr"):
                buf.append("\n")
            elif tag in (f"{A}blip", f"{V}imagedata"):
                rid = next(
                    (node.get(a) for a in IMAGE_REFS[tag] if node.get(a)), None
                )
                img = self._pkg.image(rid, self._part)
                if img is not None:
                    yield from flush()
                    yield replace(img, order=next(order))

        yield from flush()

    def _table(self, tbl: ET.Element):
        rows: list[list[str]] = []
        pending: list[ImageBlock] = []

        for tr in tbl.findall(f"{W}tr"):
            row: list[str] = []
            for tc in tr.findall(f"{W}tc"):
                cell: list[str] = []
                for child in tc:
                    if child.tag == f"{W}p":
                        blocks = self._paragraph(child, "table")
                    elif child.tag == f"{W}tbl":
                        blocks = self._table(child)
                    else:
                        continue
                    for block in blocks:
                        if isinstance(block, ImageBlock):
                            pending.append(block)
                        else:
                            cell.append(block.text)
                row.append("\n".join(cell))
            if row:
                rows.append(row)

        if rows:
            order = next(self._pkg.order)
            self.tables.append(Table(rows=rows, order=order))
            yield TextBlock(render_rows(rows), "table", order)

        yield from pending


def render_rows(rows: list[list[str]]) -> str:
    """Flatten table rows into pipe-delimited text for the text layer."""
    return "\n".join(" | ".join(c.replace("\n", " ") for c in r) for r in rows)
