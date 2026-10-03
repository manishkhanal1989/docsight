"""LibreOffice bridge.

Two jobs only, both of which nothing in pip can do:

1. Legacy OLE2 Office formats -- ``.doc``, ``.xls``, ``.ppt``. No
   pure-Python reader handles their binary record streams.
2. Faithful page rendering of any Office document, which needs real layout:
   text wrap, floating image position, page breaks, EMF/WMF metafiles.

LibreOffice is a system install, not a dependency pip can resolve, so every
entry point here fails with an actionable message rather than a traceback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import ConversionFailed, MissingDependency

#: Override discovery with DOCSIGHT_SOFFICE=/path/to/soffice
ENV_VAR = "DOCSIGHT_SOFFICE"

_CANDIDATES = (
    "soffice",
    "soffice.exe",
    "libreoffice",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/lib/libreoffice/program/soffice",
    "/opt/libreoffice/program/soffice",
)

_HINT = (
    "Install LibreOffice (https://www.libreoffice.org/download/), or point "
    f"{ENV_VAR} at an existing soffice binary. In Docker: "
    "apt-get install -y libreoffice-writer"
)


def find_soffice() -> str | None:
    """Locate the LibreOffice binary, or None if it is not installed."""
    override = os.environ.get(ENV_VAR)
    if override and Path(override).exists():
        return override
    for cand in _CANDIDATES:
        found = shutil.which(cand) if not os.path.isabs(cand) else (
            cand if Path(cand).exists() else None
        )
        if found:
            return found
    return None


def available() -> bool:
    """Whether conversion and page rendering of Word files is possible here."""
    return find_soffice() is not None


def require_soffice() -> str:
    exe = find_soffice()
    if exe is None:
        raise MissingDependency("LibreOffice (soffice)", _HINT)
    return exe


def to_pdf(path: str | Path, outdir: str | Path | None = None, timeout: int = 180) -> Path:
    """Convert any LibreOffice-readable document to PDF; returns the path.

    When *outdir* is omitted the PDF lands in a temporary directory that the
    caller owns and should clean up.
    """
    exe = require_soffice()
    path = Path(path).resolve()
    outdir = Path(outdir) if outdir else Path(tempfile.mkdtemp(prefix="docsight-"))
    outdir.mkdir(parents=True, exist_ok=True)

    # A private user profile keeps concurrent conversions from fighting over
    # the default one, which otherwise makes soffice exit without converting.
    profile = outdir / ".lo-profile"
    cmd = [
        exe,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        f"-env:UserInstallation=file:///{profile.as_posix().lstrip('/')}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(path),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionFailed(
            f"LibreOffice timed out after {timeout}s converting {path.name}"
        ) from exc

    pdf = outdir / (path.stem + ".pdf")
    if not pdf.exists():
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise ConversionFailed(
            f"LibreOffice produced no PDF for {path.name} "
            f"(exit {proc.returncode}){': ' + detail if detail else ''}"
        )
    return pdf


def extract(path: str | Path, *, embedded_images: bool = True, max_pages: int | None = None):
    """Route a legacy OLE2 Office file through PDF, then parse it as a PDF.

    Serves .doc, .xls and .ppt alike. The round trip costs image fidelity --
    LibreOffice re-encodes what it rasterises -- so the warning says so
    rather than letting a caller assume the bytes are original.
    """
    from ..routing import detect
    from . import pdf as pdf_backend

    src = Path(path)
    kind = detect(src)
    converted = to_pdf(src)
    try:
        doc = pdf_backend.extract(
            converted, embedded_images=embedded_images, max_pages=max_pages
        )
    finally:
        shutil.rmtree(converted.parent, ignore_errors=True)

    doc.path = src
    doc.kind = kind
    doc.warnings.append(
        f"legacy {kind.value} was converted to PDF by LibreOffice before "
        "extraction; image bytes are re-encoded rather than original"
    )
    return doc
