"""Format detection by content, with extension only as a tie-breaker."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .types import DocKind

_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x01\x00\x00\x00", "image/x-emf"),
    (b"\xd7\xcd\xc6\x9a", "image/x-wmf"),
]

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heic",
    ".emf": "image/x-emf",
    ".wmf": "image/x-wmf",
    ".svg": "image/svg+xml",
}

#: Formats a vision model cannot consume directly -- they must be
#: rasterised first. Word stores pasted clipboard graphics as metafiles
#: surprisingly often, and they silently vanish from naive pipelines.
VECTOR_MIMES = frozenset({"image/x-emf", "image/x-wmf", "image/svg+xml"})

#: OPC marker part -> format. Identifying by the part that must exist beats
#: trusting [Content_Types].xml, which a renamed file may still carry.
_OPC_MARKERS = {
    "word/document.xml": DocKind.DOCX,
    "xl/workbook.xml": DocKind.XLSX,
    "ppt/presentation.xml": DocKind.PPTX,
}

#: Stream names inside an OLE2 compound file, UTF-16LE as stored in the
#: directory. Discriminating by content means a .doc renamed to .xls still
#: routes correctly.
_OLE_STREAMS: list[tuple[bytes, DocKind]] = [
    ("WordDocument", DocKind.DOC),
    ("Workbook", DocKind.XLS),
    ("Book", DocKind.XLS),
    ("PowerPoint Document", DocKind.PPT),
]
_OLE_STREAMS = [(name.encode("utf-16-le"), kind) for name, kind in _OLE_STREAMS]  # type: ignore[misc]
_MSG_STREAM = "__substg1.0".encode("utf-16-le")

_EXT_KIND = {
    ".docx": DocKind.DOCX, ".docm": DocKind.DOCX,
    ".xlsx": DocKind.XLSX, ".xlsm": DocKind.XLSX,
    ".pptx": DocKind.PPTX, ".pptm": DocKind.PPTX,
    ".doc": DocKind.DOC, ".xls": DocKind.XLS, ".ppt": DocKind.PPT,
    ".odt": DocKind.ODT, ".ods": DocKind.ODS, ".odp": DocKind.ODP,
    ".pdf": DocKind.PDF, ".rtf": DocKind.RTF,
    ".html": DocKind.HTML, ".htm": DocKind.HTML, ".xhtml": DocKind.HTML,
    ".txt": DocKind.TEXT, ".csv": DocKind.TEXT, ".tsv": DocKind.TEXT,
    ".md": DocKind.TEXT, ".log": DocKind.TEXT, ".json": DocKind.TEXT,
    ".eml": DocKind.EMAIL, ".mht": DocKind.HTML, ".mhtml": DocKind.HTML,
    ".zip": DocKind.ARCHIVE,
}

#: Formats docsight recognises but cannot read, with the reason. Naming them
#: explicitly beats reporting UNKNOWN, which reads like a corrupt file.
UNREADABLE: dict[str, str] = {
    ".msg": (
        "Outlook .msg is an OLE2 container with no stdlib reader. Convert it "
        "to .eml, or install extract-msg and register a backend for it."
    ),
    ".pages": "Apple Pages is a proprietary bundle; export to .docx or PDF.",
    ".numbers": "Apple Numbers is a proprietary bundle; export to .xlsx or PDF.",
    ".key": "Apple Keynote is a proprietary bundle; export to .pptx or PDF.",
    ".7z": "7-Zip archives need py7zr; only .zip is built in.",
    ".rar": "RAR archives need rarfile plus an unrar binary.",
}

_HTML_HINT = re.compile(rb"<\s*(!doctype\s+html|html|head|body|table|div|p|img)\b", re.I)


def sniff_mime(data: bytes, name: str = "") -> str:
    """Best-effort MIME type for raw image bytes."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1"):
        return "image/heic"
    if data.lstrip()[:5] in (b"<svg ", b"<?xml"):
        return "image/svg+xml"
    return _EXT_MIME.get(Path(name).suffix.lower(), "application/octet-stream")


def detect(path: str | Path) -> DocKind:
    """Classify a file on disk by its content.

    Extension is consulted only where bytes cannot decide -- plain text
    versus CSV, or an OLE2 file whose directory is past the sniffed window.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    with open(path, "rb") as fh:
        head = fh.read(65536)

    if head.startswith(b"%PDF"):
        return DocKind.PDF
    if head.startswith(b"{\\rt"):
        return DocKind.RTF
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _ole_kind(head, suffix)
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(path, suffix)
    if sniff_mime(head, path.name).startswith("image/"):
        return DocKind.IMAGE
    if _is_email(head):
        return DocKind.EMAIL
    if _HTML_HINT.search(head[:8192]):
        return DocKind.HTML
    if _looks_textual(head):
        return DocKind.TEXT
    return _EXT_KIND.get(suffix, DocKind.UNKNOWN)


def unreadable_reason(path: str | Path) -> str | None:
    """Why a recognised-but-unsupported file cannot be read, or None."""
    return UNREADABLE.get(Path(path).suffix.lower())


def _zip_kind(path: Path, suffix: str) -> DocKind:
    """Tell the zip-based formats apart by the parts they must contain."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for marker, kind in _OPC_MARKERS.items():
                if marker in names:
                    return kind
            if "content.xml" in names:
                from .backends.odf import detect_kind

                return detect_kind(path) or DocKind.ODT
    except (zipfile.BadZipFile, OSError):
        return DocKind.UNKNOWN
    # A plain zip: treat it as a bundle of documents rather than a document.
    return DocKind.ARCHIVE if suffix in (".zip", "") else DocKind.UNKNOWN


def _ole_kind(head: bytes, suffix: str) -> DocKind:
    """Discriminate legacy OLE2 files by their directory stream names.

    The directory can sit beyond the sniffed window in a large file, so the
    extension is the fallback rather than the primary signal.
    """
    if _MSG_STREAM in head:
        return DocKind.UNKNOWN  # .msg; unreadable_reason() explains why
    for needle, kind in _OLE_STREAMS:
        if needle in head:
            return kind
    return _EXT_KIND.get(suffix, DocKind.DOC)


def _is_email(head: bytes) -> bool:
    """RFC 5322 messages open with headers, possibly after a From_ line."""
    window = head[:2048]
    if window.startswith(b"From "):
        return True
    required = (b"Subject:", b"From:", b"To:", b"Date:", b"Received:", b"Message-ID:")
    hits = sum(1 for h in required if re.search(rb"^" + h, window, re.M))
    return hits >= 2 and b"MIME-Version:" in head or hits >= 3


def _looks_textual(head: bytes) -> bool:
    """Whether the bytes decode as text with few control characters."""
    if not head:
        return False
    if b"\x00" in head[:1024]:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("cp1252")
        except UnicodeDecodeError:
            return False
    control = sum(1 for b in head if b < 9 or (13 < b < 32))
    return control / len(head) < 0.05
