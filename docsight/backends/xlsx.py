"""Excel OOXML (.xlsx/.xlsm) extraction, standard library only.

Two things make spreadsheets different from Word documents:

* Cell text is indirected through a shared string table, so reading
  worksheet XML alone yields numbers and indices, not words.
* Pictures are never referenced from worksheet XML. The sheet names a
  *drawing* part, and the drawing holds the image relationships -- so a
  walker that only reads the sheet sees no images at all. This is the usual
  reason "my xlsx has a scan in it but nothing came out".
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

from ..types import DocKind, Document, Table, TextBlock
from .opc import OpcPackage

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

MARKER = "xl/workbook.xml"
MEDIA = "xl/media/"

#: Cap on scanned rows per sheet. A verification document holds tens of
#: rows; a 500k-row data export would otherwise dominate the text layer and
#: the token budget with no benefit.
MAX_ROWS = 2000
MAX_COLS = 64


def extract(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse an .xlsx into per-sheet tables, text, and image blocks.

    *max_pages* caps the number of worksheets read.
    """
    path = Path(path)
    doc = Document(path=path, kind=DocKind.XLSX)

    with OpcPackage(path) as pkg:
        doc.meta.update(pkg.core_properties())
        strings = _shared_strings(pkg)
        sheets = _sheet_parts(pkg)
        if max_pages is not None:
            sheets = sheets[:max_pages]
        doc.meta["sheet_count"] = len(sheets)
        doc.meta["sheet_names"] = [name for name, _ in sheets]

        for name, part in sheets:
            root = pkg.xml(part)
            if root is None:
                continue

            rows, truncated = _rows(root, strings)
            if rows:
                order = next(pkg.order)
                doc.tables.append(Table(rows=rows, order=order, section=name))
                body = "\n".join(
                    " | ".join(c for c in r if c) for r in rows if any(r)
                )
                if body.strip():
                    doc.blocks.append(
                        TextBlock(f"[{name}]\n{body}", "sheet", order, section=name)
                    )
            if truncated:
                doc.warnings.append(
                    f"sheet {name!r} exceeded {MAX_ROWS} rows; text layer truncated"
                )

            if embedded_images:
                for img in pkg.drawing_images(part, section=name):
                    doc.blocks.append(replace(img, order=next(pkg.order)))

        if embedded_images:
            doc.blocks.extend(pkg.orphan_media(MEDIA))
        pkg.note_embeddings("xl")
        doc.warnings.extend(pkg.warnings)

    return doc


def _shared_strings(pkg: OpcPackage) -> list[str]:
    """The workbook string table, which cells reference by index."""
    root = pkg.xml("xl/sharedStrings.xml")
    if root is None:
        return []
    out = []
    for si in root.findall(f"{S}si"):
        # Rich text splits one string across several <t> runs.
        out.append("".join(t.text or "" for t in si.iter(f"{S}t")))
    return out


def _sheet_parts(pkg: OpcPackage) -> list[tuple[str, str]]:
    """``(sheet name, part path)`` in workbook tab order.

    Tab order comes from workbook.xml; falling back to filename order would
    mislabel sheets, which matters when a prompt says "the Spouse tab".
    """
    root = pkg.xml(MARKER)
    if root is not None:
        rels = pkg.rels(MARKER)
        out = []
        for sheet in root.iter(f"{S}sheet"):
            name = sheet.get("name") or "Sheet"
            rid = sheet.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/"
                "relationships}id"
            )
            target = rels.get(rid or "")
            if target and target in pkg.names:
                out.append((name, target))
        if out:
            return out
    # Malformed workbook part: fall back to whatever sheets exist.
    return [
        (Path(p).stem, p) for p in pkg.matching("xl/worksheets/", ".xml")
    ]


def _rows(root: ET.Element, strings: list[str]) -> tuple[list[list[str]], bool]:
    """Read a worksheet into dense rows of cell text."""
    rows: list[list[str]] = []
    truncated = False

    for n, row in enumerate(root.iter(f"{S}row")):
        if n >= MAX_ROWS:
            truncated = True
            break
        cells: list[str] = []
        for cell in row.iter(f"{S}c"):
            if len(cells) >= MAX_COLS:
                break
            cells.append(_cell_text(cell, strings))
        # Trailing empties carry no information and bloat the text layer.
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(cells)
    return rows, truncated


def _cell_text(cell: ET.Element, strings: list[str]) -> str:
    """One cell's display text, resolving the shared string table."""
    ctype = cell.get("t")

    if ctype == "inlineStr":
        node = cell.find(f"{S}is")
        return "".join(t.text or "" for t in node.iter(f"{S}t")) if node is not None else ""

    v = cell.find(f"{S}v")
    raw = v.text if v is not None else None
    if raw is None:
        return ""

    if ctype == "s":  # index into the shared string table
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "TRUE" if raw == "1" else "FALSE"
    # Numbers, dates and formula results all surface as their stored value.
    # Dates stay as serial numbers: guessing a format from the style index
    # would invent precision the file does not carry.
    return raw
