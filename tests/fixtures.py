"""Synthetic .docx builders.

Written by hand rather than with python-docx so the tests can produce the
awkward shapes that matter -- anchored images, VML shapes, orphaned media,
tracked deletions -- which a well-behaved writer library will not emit.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="emf" ContentType="image/x-emf"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

CORE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
    xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:dcterms="http://purl.org/dc/terms/">
  <dc:title>Certificate of Marriage</dc:title>
  <dc:creator>County Clerk</dc:creator>
  <cp:lastModifiedBy>HR Intake</cp:lastModifiedBy>
  <dcterms:created>2024-03-11T09:14:00Z</dcterms:created>
  <dcterms:modified>2024-03-12T16:02:00Z</dcterms:modified>
</cp:coreProperties>"""

DOC_OPEN = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
    xmlns:v="urn:schemas-microsoft-com:vml">
  <w:body>"""

DOC_CLOSE = """  </w:body>
</w:document>"""


def para(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def deleted_para(text: str) -> str:
    """A tracked deletion -- must never appear in extracted text."""
    return f'<w:p><w:del><w:r><w:delText xml:space="preserve">{text}</w:delText></w:r></w:del></w:p>'


def _blip(rid: str) -> str:
    return (
        "<a:graphic><a:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill></pic:pic>'
        "</a:graphicData></a:graphic>"
    )


def inline_image(rid: str) -> str:
    return (
        f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="5400000" cy="3600000"/>'
        f"{_blip(rid)}</wp:inline></w:drawing></w:r></w:p>"
    )


def anchored_image(rid: str) -> str:
    """A *floating* image -- how a pasted scan usually lands in Word, and the
    shape that python-docx's inline_shapes cannot see."""
    return (
        f'<w:p><w:r><w:drawing><wp:anchor behindDoc="1" relativeHeight="1">'
        f'<wp:extent cx="7000000" cy="9000000"/>{_blip(rid)}'
        "</wp:anchor></w:drawing></w:r></w:p>"
    )


def vml_image(rid: str) -> str:
    """A legacy VML shape, still produced by old templates and some scanners."""
    return (
        f"<w:p><w:r><w:pict><v:shape><v:imagedata r:id=\"{rid}\"/>"
        "</v:shape></w:pict></w:r></w:p>"
    )


def caption_then_image(caption: str, rid: str) -> str:
    """Text and an image inside one paragraph -- tests encounter ordering."""
    return (
        f'<w:p><w:r><w:t xml:space="preserve">{caption}</w:t></w:r>'
        f"<w:r><w:drawing><wp:inline>{_blip(rid)}</wp:inline></w:drawing></w:r></w:p>"
    )


def table(rows: list[list[str]]) -> str:
    out = ["<w:tbl>"]
    for row in rows:
        out.append("<w:tr>")
        for cell in row:
            out.append(f"<w:tc>{para(cell)}</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def png(width: int, height: int, *, color: bytes = b"\xc8\xc8\xc8") -> bytes:
    """A real, decodable PNG of the requested size.

    Byte length scales with area, so size-based filtering can be exercised
    honestly rather than against a stub.
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + color * width
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def emf(size: int = 4096) -> bytes:
    """An EMF metafile header followed by filler.

    Enough to be detected and refused by the payload builder, which is the
    behaviour under test; it is not a renderable metafile.
    """
    return b"\x01\x00\x00\x00" + b"\x00" * (size - 4)


def build_docx(
    path: Path,
    body: str,
    media: dict[str, bytes] | None = None,
    rels: dict[str, str] | None = None,
    *,
    extra_parts: dict[str, bytes] | None = None,
) -> Path:
    """Assemble a .docx from body XML, media parts, and relationships.

    *rels* maps relationship id -> target relative to ``word/``.
    """
    media = media or {}
    rels = rels or {}

    rel_xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for rid, target in rels.items():
        rel_xml.append(
            f'<Relationship Id="{rid}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="{target}"/>'
        )
    rel_xml.append("</Relationships>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("docProps/core.xml", CORE_XML)
        z.writestr("word/document.xml", DOC_OPEN + body + DOC_CLOSE)
        z.writestr("word/_rels/document.xml.rels", "\n".join(rel_xml))
        for name, data in media.items():
            z.writestr(f"word/media/{name}", data)
        for name, data in (extra_parts or {}).items():
            z.writestr(name, data)
    return path
