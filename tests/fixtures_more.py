"""Builders for the non-Word formats.

Hand-assembled for the same reason as the Word fixtures: a well-behaved
writer library will not emit the awkward shapes that break extractors --
pictures reachable only through a drawing part, hex-encoded RTF payloads,
data: URIs, archive bombs.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from tests.fixtures import png

# --- xlsx -----------------------------------------------------------------

_XL_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{target}"/>
</Relationships>"""

S_NS = (
    'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def build_xlsx(
    path: Path,
    sheets: dict[str, list[list[str]]],
    *,
    image: bytes | None = None,
    image_on: str | None = None,
) -> Path:
    """Build an .xlsx with shared strings and, optionally, a picture.

    The picture is attached the way Excel really does it -- through a
    drawing part -- so a backend that only reads worksheet XML finds
    nothing.
    """
    strings: list[str] = []

    def sid(value: str) -> int:
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    sheet_parts: dict[str, str] = {}
    sheet_xml: dict[str, str] = {}
    for n, (name, rows) in enumerate(sheets.items(), start=1):
        part = f"xl/worksheets/sheet{n}.xml"
        sheet_parts[name] = part
        body = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{chr(ord('A') + c)}{r}"
                if value == "":
                    continue
                cells.append(f'<c r="{ref}" t="s"><v>{sid(value)}</v></c>')
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
        drawing = (
            '<drawing r:id="rIdD1"/>' if image is not None and image_on == name else ""
        )
        sheet_xml[part] = (
            f'<?xml version="1.0" encoding="UTF-8"?><worksheet {S_NS}>'
            f'<sheetData>{"".join(body)}</sheetData>{drawing}</worksheet>'
        )

    wb_sheets = "".join(
        f'<sheet name="{name}" sheetId="{i}" r:id="rIdS{i}"/>'
        for i, name in enumerate(sheets, start=1)
    )
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8"?><workbook {S_NS}>'
        f"<sheets>{wb_sheets}</sheets></workbook>"
    )
    wb_rels = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, name in enumerate(sheets, start=1):
        target = Path(sheet_parts[name]).name
        wb_rels.append(
            f'<Relationship Id="rIdS{i}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/worksheet" Target="worksheets/{target}"/>'
        )
    wb_rels.append("</Relationships>")

    si = "".join(f"<si><t>{s}</t></si>" for s in strings)
    shared = (
        f'<?xml version="1.0" encoding="UTF-8"?><sst {S_NS} count="{len(strings)}" '
        f'uniqueCount="{len(strings)}">{si}</sst>'
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _XL_CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS.format(target="xl/workbook.xml"))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
        z.writestr("xl/sharedStrings.xml", shared)
        for part, xml in sheet_xml.items():
            z.writestr(part, xml)

        if image is not None and image_on:
            part = sheet_parts[image_on]
            z.writestr(
                f"xl/worksheets/_rels/{Path(part).name}.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdD1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>'
                "</Relationships>",
            )
            z.writestr(
                "xl/drawings/drawing1.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<wsDr xmlns="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<twoCellAnchor><pic><blipFill><a:blip r:embed="rIdI1"/></blipFill></pic>'
                "</twoCellAnchor></wsDr>",
            )
            z.writestr(
                "xl/drawings/_rels/drawing1.xml.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdI1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/image" Target="../media/image1.png"/>'
                "</Relationships>",
            )
            z.writestr("xl/media/image1.png", image)
    return path


# --- pptx -----------------------------------------------------------------

_PPT_CONTENT_TYPES = _XL_CONTENT_TYPES.replace(
    "/xl/workbook.xml",
    "/ppt/presentation.xml",
).replace(
    "spreadsheetml.sheet.main+xml",
    "presentationml.presentation.main+xml",
)

A_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def build_pptx(
    path: Path,
    slides: list[tuple[list[str], bytes | None]],
    *,
    notes: dict[int, str] | None = None,
) -> Path:
    """Build a .pptx from ``(lines, optional image bytes)`` per slide."""
    notes = notes or {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _PPT_CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS.format(target="ppt/presentation.xml"))
        z.writestr(
            "ppt/presentation.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><p:presentation {A_NS}/>',
        )
        for i, (lines, image) in enumerate(slides, start=1):
            paras = "".join(
                f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines
            )
            pic = (
                '<p:pic><p:blipFill><a:blip r:embed="rIdI1"/></p:blipFill></p:pic>'
                if image is not None
                else ""
            )
            z.writestr(
                f"ppt/slides/slide{i}.xml",
                f'<?xml version="1.0" encoding="UTF-8"?><p:sld {A_NS}>'
                f"<p:cSld><p:spTree>{paras}{pic}</p:spTree></p:cSld></p:sld>",
            )
            rels = ['<?xml version="1.0" encoding="UTF-8"?>',
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
            if image is not None:
                z.writestr(f"ppt/media/image{i}.png", image)
                rels.append(
                    f'<Relationship Id="rIdI1" Type="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/relationships/image" Target="../media/image{i}.png"/>'
                )
            if i in notes:
                z.writestr(
                    f"ppt/notesSlides/notesSlide{i}.xml",
                    f'<?xml version="1.0" encoding="UTF-8"?><p:notes {A_NS}>'
                    f"<a:p><a:r><a:t>{notes[i]}</a:t></a:r></a:p></p:notes>",
                )
                rels.append(
                    f'<Relationship Id="rIdN1" Type="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/relationships/notesSlide" '
                    f'Target="../notesSlides/notesSlide{i}.xml"/>'
                )
            rels.append("</Relationships>")
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", "".join(rels))
    return path


# --- odf ------------------------------------------------------------------

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)


def build_odt(
    path: Path,
    paragraphs: list[str],
    *,
    image: bytes | None = None,
    rows: list[list[str]] | None = None,
    mimetype: str = "application/vnd.oasis.opendocument.text",
) -> Path:
    parts = [f"<text:p>{p}</text:p>" for p in paragraphs]
    if rows:
        cells = "".join(
            "<table:table-row>"
            + "".join(f"<table:table-cell><text:p>{c}</text:p></table:table-cell>" for c in row)
            + "</table:table-row>"
            for row in rows
        )
        parts.append(f'<table:table table:name="Sheet1">{cells}</table:table>')
    if image is not None:
        parts.append('<draw:frame><draw:image xlink:href="Pictures/scan.png"/></draw:frame>')

    content = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ODF_NS}>'
        f'<office:body><office:text>{"".join(parts)}</office:text></office:body>'
        "</office:document-content>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", mimetype)
        z.writestr("content.xml", content)
        z.writestr(
            "meta.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<office:meta><dc:title>ODF Certificate</dc:title>"
            "<meta:initial-creator>Registry Office</meta:initial-creator>"
            "</office:meta></office:document-meta>",
        )
        if image is not None:
            z.writestr("Pictures/scan.png", image)
    return path


# --- rtf ------------------------------------------------------------------


def build_rtf(path: Path, text: str, *, image: bytes | None = None) -> Path:
    """Build an .rtf with optional hex-encoded PNG payload."""
    body = text.replace("\n", r"\par ")
    pict = ""
    if image is not None:
        pict = (
            r"{\pict\pngblip\picw100\pich100 " + image.hex() + "}"
        )
    rtf = (
        r"{\rtf1\ansi\deff0"
        r"{\fonttbl{\f0\fnil Calibri;}}"
        r"{\colortbl;\red0\green0\blue0;}"
        r"{\info{\title Marriage Record}{\author Registry}}"
        rf"\f0\fs22 {body}\par {pict}"
        "}"
    )
    path.write_bytes(rtf.encode("ascii", "replace"))
    return path


# --- html / email / zip ---------------------------------------------------


def build_html(path: Path, *, inline_png: bytes | None = None, sidecar: bytes | None = None) -> Path:
    import base64

    imgs = []
    if inline_png is not None:
        b64 = base64.b64encode(inline_png).decode()
        imgs.append(f'<img src="data:image/png;base64,{b64}" alt="scan">')
    if sidecar is not None:
        (path.parent / "sidecar.png").write_bytes(sidecar)
        imgs.append('<img src="sidecar.png">')
    imgs.append('<img src="https://example.invalid/tracker.png">')

    path.write_text(
        "<html><head><title>Marriage Record</title>"
        "<style>.x{color:red}</style><script>var a=1;</script></head><body>"
        "<h1>CERTIFICATE OF MARRIAGE</h1>"
        "<p>Dana Alvarez and Samira Okonkwo, 14 June 2019, Travis County, Texas.</p>"
        "<table><tr><th>Field</th><th>Value</th></tr>"
        "<tr><td>Spouse A</td><td>Dana Alvarez</td></tr>"
        "<tr><td>Spouse B</td><td>Samira Okonkwo</td></tr></table>"
        + "".join(imgs)
        + "</body></html>",
        encoding="utf-8",
    )
    return path


def build_eml(path: Path, attachments: dict[str, tuple[str, bytes]], *, body: str = "") -> Path:
    """Build an .eml with named attachments: ``{filename: (mime, bytes)}``."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "employee@example.com"
    msg["To"] = "benefits@example.com"
    msg["Subject"] = "Dependent verification documents"
    msg["Date"] = "Tue, 12 Mar 2024 16:02:00 +0000"
    msg.set_content(body or "Attached please find our marriage certificate.")

    for name, (mime, data) in attachments.items():
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    path.write_bytes(msg.as_bytes())
    return path


def build_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def build_zip_bomb(path: Path, *, size: int = 60_000_000) -> Path:
    """A highly compressible member, to exercise the ratio guard."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.txt", b"\0" * size)
    return path


def scan_png(variant: int = 0) -> bytes:
    """A plausible certificate scan: large enough to survive filtering.

    *variant* changes the pixel data, so a multi-page fixture produces
    genuinely distinct pages rather than tripping the duplicate filter.
    """
    shade = 200 - (variant * 17) % 120
    return png(1200, 1600, color=bytes([shade, shade, shade]))


def logo_png() -> bytes:
    """An agency logo: small enough to be filtered out."""
    return png(48, 48)
