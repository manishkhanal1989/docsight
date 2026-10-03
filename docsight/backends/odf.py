"""OpenDocument extraction (.odt, .ods, .odp), standard library only.

LibreOffice's native formats, and what you get when someone saves from
Google Docs as ODF. Structurally simpler than OOXML: one ``content.xml``
holds everything, and images are referenced by direct path into
``Pictures/`` rather than through a relationship table.

All three document types share this code -- the only difference is which
tag wraps a row of text.
"""

from __future__ import annotations

import itertools
import posixpath
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import CorruptDocument
from ..imagesize import probe
from ..routing import sniff_mime
from ..types import DocKind, Document, ImageBlock, Table, TextBlock
from .docx import render_rows

TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
XLINK = "{http://www.w3.org/1999/xlink}"
META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"
DC = "{http://purl.org/dc/elements/1.1/}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"

MARKER = "content.xml"
MIMETYPE = "mimetype"

_KINDS = {
    "application/vnd.oasis.opendocument.text": DocKind.ODT,
    "application/vnd.oasis.opendocument.spreadsheet": DocKind.ODS,
    "application/vnd.oasis.opendocument.presentation": DocKind.ODP,
}

MAX_ROWS = 2000


def detect_kind(path: str | Path) -> DocKind | None:
    """Read the ODF ``mimetype`` member to tell odt from ods from odp."""
    try:
        with zipfile.ZipFile(path) as z:
            if MARKER not in z.namelist():
                return None
            if MIMETYPE not in z.namelist():
                return DocKind.ODT
            return _KINDS.get(z.read(MIMETYPE).decode("ascii", "ignore").strip())
    except (zipfile.BadZipFile, OSError):
        return None


def extract(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse an ODF document into text, tables, and image blocks."""
    path = Path(path)
    kind = detect_kind(path) or DocKind.ODT
    doc = Document(path=path, kind=kind)
    order = itertools.count()

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise CorruptDocument(f"{path.name} is not a readable ODF zip: {exc}") from exc

    with zf:
        names = set(zf.namelist())
        doc.meta.update(_meta(zf, names))

        root = _xml(zf, MARKER, doc.warnings)
        consumed: set[str] = set()
        if root is not None:
            body = root.find(f"{OFFICE}body")
            doc.blocks.extend(
                _walk(body if body is not None else root, zf, names, order, consumed,
                      doc, embedded_images)
            )

        if embedded_images:
            doc.blocks.extend(_orphans(zf, names, consumed, order, doc.warnings))
        doc.meta["media_count"] = sum(1 for n in names if n.startswith("Pictures/"))

    return doc


def _walk(container, zf, names, order, consumed, doc, want_images):
    """Emit text, tables and images in document order."""
    for node in container.iter():
        tag = node.tag

        if tag == f"{TEXT}p" or tag == f"{TEXT}h":
            # Nested spans and links keep their text in descendants.
            text = "".join(node.itertext()).strip()
            if text:
                yield TextBlock(text, "paragraph", next(order))

        elif tag == f"{TABLE}table":
            rows = _rows(node)
            if rows:
                o = next(order)
                name = node.get(f"{TABLE}name")
                doc.tables.append(Table(rows=rows, order=o, section=name))
                role = "sheet" if doc.kind is DocKind.ODS else "table"
                yield TextBlock(render_rows(rows), role, o, section=name)

        elif tag == f"{DRAW}image" and want_images:
            img = _image(node, zf, names, consumed, doc.warnings)
            if img is not None:
                yield replace(img, order=next(order))


def _rows(table: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for n, tr in enumerate(table.iter(f"{TABLE}table-row")):
        if n >= MAX_ROWS:
            break
        cells = []
        for tc in tr.iter(f"{TABLE}table-cell"):
            cells.append("".join(tc.itertext()).strip())
            # A run of identical empty cells is stored once with a repeat
            # count; expanding it fully would add thousands of blanks.
            repeat = tc.get(f"{TABLE}number-columns-repeated")
            if repeat and repeat.isdigit() and cells[-1]:
                cells.extend([cells[-1]] * min(int(repeat) - 1, 32))
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(cells)
    return rows


def _image(node, zf, names, consumed, warnings) -> ImageBlock | None:
    href = node.get(f"{XLINK}href")
    if not href:
        return None
    target = href.lstrip("./")
    if target not in names:
        # ODF also allows the bytes inline as base64 in office:binary-data.
        inline = node.find(f"{OFFICE}binary-data")
        if inline is not None and inline.text:
            import base64

            try:
                data = base64.b64decode(inline.text)
            except ValueError:
                return None
            size = probe(data)
            return ImageBlock(
                data=data,
                mime=sniff_mime(data, "inline"),
                name="inline-image",
                order=-1,
                width=size[0] if size else None,
                height=size[1] if size else None,
            )
        warnings.append(f"image target not inside package: {target}")
        return None

    data = zf.read(target)
    consumed.add(target)
    size = probe(data)
    return ImageBlock(
        data=data,
        mime=sniff_mime(data, target),
        name=posixpath.basename(target),
        order=-1,
        width=size[0] if size else None,
        height=size[1] if size else None,
    )


def _orphans(zf, names, consumed, order, warnings) -> list[ImageBlock]:
    out = []
    for name in sorted(n for n in names if n.startswith("Pictures/")):
        if name in consumed:
            continue
        data = zf.read(name)
        if not data:
            continue
        size = probe(data)
        out.append(
            ImageBlock(
                data=data,
                mime=sniff_mime(data, name),
                name=posixpath.basename(name),
                order=next(order),
                width=size[0] if size else None,
                height=size[1] if size else None,
                referenced=False,
            )
        )
    if out:
        warnings.append(
            f"{len(out)} picture(s) unreferenced by content.xml -- included "
            "with referenced=False"
        )
    return out


def _xml(zf, part, warnings) -> ET.Element | None:
    try:
        return ET.fromstring(zf.read(part))
    except (KeyError, ET.ParseError) as exc:
        warnings.append(f"{part}: unreadable ({exc})")
        return None


def _meta(zf, names) -> dict[str, object]:
    if "meta.xml" not in names:
        return {}
    try:
        root = ET.fromstring(zf.read("meta.xml"))
    except ET.ParseError:
        return {}
    fields = {
        "title": f"{DC}title",
        "author": f"{META}initial-creator",
        "last_modified_by": f"{DC}creator",
        "created": f"{META}creation-date",
        "modified": f"{DC}date",
    }
    out: dict[str, object] = {}
    for key, tag in fields.items():
        el = next(root.iter(tag), None)
        if el is not None and el.text:
            out[key] = el.text
    return out
