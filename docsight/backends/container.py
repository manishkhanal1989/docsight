"""Containers: email messages and archives.

These hold *other* documents rather than content of their own, which is how
evidence usually arrives -- a forwarded email with the certificate
attached, or ``my documents.zip``. Each member is extracted as its own
:class:`~docsight.types.Document` and hung off ``children``, so three
attachments stay three distinguishable documents instead of one blur.

Both backends are hardened against hostile archives: member count, total
uncompressed size, compression ratio, and nesting depth are all capped, and
path traversal in member names cannot escape the extraction directory.
"""

from __future__ import annotations

import itertools
import shutil
import tempfile
import zipfile
from email import message_from_binary_file, policy
from email.message import EmailMessage
from pathlib import Path

from ..errors import CorruptDocument
from ..types import DocKind, Document, TextBlock

#: Guards against archive bombs. A zip of dependent documents is a handful
#: of files and a few hundred megabytes at the very most.
MAX_MEMBERS = 200
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RATIO = 200  # uncompressed:compressed
MAX_DEPTH = 3  # a zip in an email in a zip is already suspicious

_SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


def extract_email(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
    _depth: int = 0,
) -> Document:
    """Parse an .eml message: headers as text, attachments as children."""
    path = Path(path)
    doc = Document(path=path, kind=DocKind.EMAIL)
    order = itertools.count()

    with open(path, "rb") as fh:
        try:
            msg: EmailMessage = message_from_binary_file(fh, policy=policy.default)
        except Exception as exc:
            raise CorruptDocument(f"{path.name} is not a readable email: {exc}") from exc

    for key in ("From", "To", "Cc", "Subject", "Date"):
        value = msg.get(key)
        if value:
            doc.meta[key.lower()] = str(value)

    header_text = "\n".join(
        f"{k}: {doc.meta[k.lower()]}"
        for k in ("From", "To", "Subject", "Date")
        if k.lower() in doc.meta
    )
    if header_text:
        doc.blocks.append(TextBlock(header_text, "subject", next(order)))

    body = _best_body(msg)
    if body and body.strip():
        doc.blocks.append(TextBlock(body.strip(), "paragraph", next(order)))

    if _depth >= MAX_DEPTH:
        doc.warnings.append(f"nesting depth {MAX_DEPTH} reached; attachments not opened")
        return doc

    workdir = Path(tempfile.mkdtemp(prefix="docsight-eml-"))
    try:
        doc.children.extend(
            _children_from_parts(msg, workdir, doc, embedded_images, max_pages, _depth)
        )
    finally:
        # Children hold their bytes in memory, so the scratch files can go.
        shutil.rmtree(workdir, ignore_errors=True)

    doc.meta["attachment_count"] = len(doc.children)
    return doc


def _children_from_parts(msg, workdir, doc, want_images, max_pages, depth):
    """Write each attachment to scratch and extract it as its own document."""
    from ..api import extract as extract_any

    count = 0
    total = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        # Inline images with no filename are usually signature logos; keep
        # them only when they are plausibly a document.
        if disposition not in ("attachment", "inline") and not filename:
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception as exc:
            doc.warnings.append(f"attachment could not be decoded: {exc}")
            continue
        if not payload:
            continue

        count += 1
        total += len(payload)
        if count > MAX_MEMBERS or total > MAX_TOTAL_BYTES:
            doc.warnings.append(
                "attachment limits exceeded; remaining attachments not opened"
            )
            break

        name = _safe_name(filename or f"part{count}")
        if name.lower() in _SKIP_NAMES:
            continue
        target = workdir / name
        target.write_bytes(payload)

        child = _extract_child(
            extract_any, target, name, doc, want_images, max_pages, depth
        )
        if child is not None:
            yield child


def extract_archive(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
    _depth: int = 0,
) -> Document:
    """Parse a .zip: every member becomes a child document."""
    path = Path(path)
    doc = Document(path=path, kind=DocKind.ARCHIVE)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise CorruptDocument(f"{path.name} is not a readable zip: {exc}") from exc

    if _depth >= MAX_DEPTH:
        doc.warnings.append(f"nesting depth {MAX_DEPTH} reached; members not opened")
        zf.close()
        return doc

    workdir = Path(tempfile.mkdtemp(prefix="docsight-zip-"))
    try:
        with zf:
            total = 0
            count = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name or name.lower() in _SKIP_NAMES or name.startswith("."):
                    continue

                count += 1
                if count > MAX_MEMBERS:
                    doc.warnings.append(
                        f"archive holds more than {MAX_MEMBERS} files; "
                        "remaining members not opened"
                    )
                    break

                if info.compress_size and (
                    info.file_size / max(info.compress_size, 1) > MAX_RATIO
                ):
                    doc.warnings.append(
                        f"{info.filename}: compression ratio over {MAX_RATIO}:1, "
                        "skipped as a likely archive bomb"
                    )
                    continue

                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    doc.warnings.append(
                        "archive exceeds the uncompressed size cap; "
                        "remaining members not opened"
                    )
                    break

                target = workdir / _safe_name(name)
                try:
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 64)
                except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                    # RuntimeError is what zipfile raises for an encrypted member.
                    doc.warnings.append(f"{info.filename}: could not read ({exc})")
                    continue

                from ..api import extract as extract_any

                child = _extract_child(
                    extract_any,
                    target,
                    info.filename,
                    doc,
                    embedded_images,
                    max_pages,
                    _depth,
                )
                if child is not None:
                    doc.children.append(child)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    doc.meta["member_count"] = len(doc.children)
    return doc


def _extract_child(extract_any, target, origin, doc, want_images, max_pages, depth):
    """Extract one container member, recording failures rather than raising.

    One unreadable attachment must not lose the other four.
    """
    from ..errors import DocSightError

    try:
        child = extract_any(
            target,
            embedded_images=want_images,
            max_pages=max_pages,
            _depth=depth + 1,
        )
    except (DocSightError, FileNotFoundError, OSError) as exc:
        doc.warnings.append(f"{origin}: skipped ({type(exc).__name__}: {exc})")
        return None
    child.origin = origin
    return child


def _best_body(msg: EmailMessage) -> str:
    """The message body, preferring plain text over HTML."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        return ""
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        return ""
    if not isinstance(content, str):
        return ""
    if part.get_content_subtype() == "html":
        return _strip_tags(content)
    return content


def _strip_tags(html: str) -> str:
    from .markup import _Harvester

    parser = _Harvester(base_dir=Path("."), want_images=False)
    parser.feed(html)
    parser.close()
    return parser.text()


def _safe_name(name: str) -> str:
    """Flatten a member name to a single safe filename.

    Takes only the basename, so ``../../etc/passwd`` and absolute paths
    cannot escape the scratch directory.
    """
    base = Path(name.replace("\\", "/")).name
    cleaned = "".join(c for c in base if c.isalnum() or c in " ._-()[]").strip()
    return (cleaned or "attachment")[:120]
