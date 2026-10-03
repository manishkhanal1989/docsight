"""Prove the library works in *this* environment.

Written for locked-down machines: an air-gapped box, a corporate laptop
with no pip, a container that may or may not have LibreOffice. It builds a
document in a temporary directory using only the standard library, runs it
through the full pipeline, and reports what is available and what is not --
so "can I actually use this here?" has a one-command answer.

    python -m docsight --selfcheck
"""

from __future__ import annotations

import platform
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

MIN_PYTHON = (3, 10)

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def _png(width: int, height: int) -> bytes:
    """A real, decodable PNG built with zlib alone."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = (b"\x00" + b"\xc8\xc8\xc8" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def _sample_docx(path: Path) -> Path:
    """A .docx holding a *floating* scan -- the shape that matters."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "word/document.xml",
            f"<w:document {_NS}><w:body>"
            "<w:p><w:r><w:t>Scanned record, attached.</w:t></w:r></w:p>"
            '<w:p><w:r><w:drawing><wp:anchor behindDoc="1">'
            '<a:graphic><a:graphicData uri="x"><pic:pic><pic:blipFill>'
            '<a:blip r:embed="rIdImg"/></pic:blipFill></pic:pic>'
            "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>"
            "</w:body></w:document>",
        )
        z.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdImg" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/image" Target="media/scan.png"/>'
            "</Relationships>",
        )
        z.writestr("word/media/scan.png", _png(1200, 1600))
    return path


def run(verbose: bool = True) -> bool:
    """Exercise the pipeline here and now. Returns whether the core works."""
    ok = True
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)

    # -- environment ---------------------------------------------------

    import docsight

    say(f"docsight {docsight.__version__}")
    say(f"  loaded from   {Path(docsight.__file__).parent}")
    say(f"  python        {sys.version.split()[0]} ({platform.python_implementation()})")
    say(f"  platform      {platform.system()} {platform.release()} / {platform.machine()}")
    say()

    if sys.version_info < MIN_PYTHON:
        say(
            f"  FAIL  python {'.'.join(map(str, MIN_PYTHON))}+ is required; "
            f"this is {sys.version.split()[0]}"
        )
        if verbose:
            print("\n".join(lines))
        return False

    # -- optional dependencies ----------------------------------------

    say("Optional extras (core formats need none of these):")
    for module, what in (
        ("pymupdf", "PDF text and images, page rendering"),
        ("PIL", "TIFF splitting, downscaling, format conversion"),
        ("pillow_heif", "HEIC from iPhone uploads"),
        ("openai", "docsight.read() convenience client"),
    ):
        present = _importable(module)
        say(f"  {'yes' if present else 'no ':<4} {module:<12} {what}")

    from .backends import office

    soffice = office.find_soffice()
    say(
        f"  {'yes' if soffice else 'no ':<4} {'LibreOffice':<12} "
        f"legacy .doc/.xls/.ppt, Office page rendering"
        + (f"\n       -> {soffice}" if soffice else "")
    )
    say()

    # -- the actual pipeline -------------------------------------------

    say("Pipeline check (synthetic .docx with a floating scan):")
    leaked: set[str] = set()
    try:
        with tempfile.TemporaryDirectory(prefix="docsight-selfcheck-") as tmp:
            sample = _sample_docx(Path(tmp) / "sample.docx")

            kind = docsight.detect(sample)
            ok &= _check(say, "detect format by content", kind.value == "docx", kind.value)

            doc = docsight.extract(sample)
            ok &= _check(say, "extract text", "Scanned record" in doc.text)
            ok &= _check(
                say,
                "find the floating (anchored) image",
                len(doc.images) == 1,
                f"{len(doc.images)} image(s)",
            )
            if doc.images:
                img = doc.images[0]
                ok &= _check(
                    say,
                    "read image dimensions without Pillow",
                    (img.width, img.height) == (1200, 1600),
                    f"{img.width}x{img.height}",
                )

            ready = docsight.prepare(sample, render_fallback=False)
            ok &= _check(
                say,
                "route to a strategy",
                ready.strategy.value == "image_only",
                ready.strategy.value,
            )

            content = ready.to_content("Read this document.")
            images = [p for p in content if p.get("type") == "image_url"]
            ok &= _check(
                say,
                "build an OpenAI-compatible payload",
                len(images) == 1,
                f"{len(content)} parts, {len(images)} image",
            )

            anthropic = ready.to_content("Read this.", dialect="anthropic")
            ok &= _check(
                say,
                "build an Anthropic-compatible payload",
                any(p.get("type") == "image" for p in anthropic),
            )

            leaked = {k.split(".")[0] for k in sys.modules} & {
                "PIL", "pymupdf", "fitz", "docx", "openpyxl", "openai", "lxml"
            }
    except Exception as exc:  # the whole point is to report, not raise
        ok = False
        say(f"  FAIL  pipeline raised {type(exc).__name__}: {exc}")

    say()
    say("Formats available here:")
    say("  " + ", ".join(k.value for k in docsight.supported_kinds()))
    if leaked:
        say(f"\n  note: third-party modules were imported: {sorted(leaked)}")
    say()
    say(
        "RESULT: core pipeline works here."
        if ok
        else "RESULT: something is wrong -- see the FAIL lines above."
    )

    if verbose:
        print("\n".join(lines))
    return ok


def _check(say, label: str, passed: bool, detail: str = "") -> bool:
    say(f"  {'ok  ' if passed else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    return passed


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
