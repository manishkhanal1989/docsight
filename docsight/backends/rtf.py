"""RTF extraction, standard library only.

RTF turns up constantly from older government and court systems, and from
anything that exports "Word-compatible" without writing real OOXML. No
maintained pip package extracts both its text and its embedded images, so
this parses the format directly.

The format is a brace-nested control-word stream. Images live in ``\\pict``
groups as hex-encoded (or occasionally binary) bytes, with the original
format named by a sibling control word -- ``\\pngblip``, ``\\jpegblip``,
``\\wmetafile``, ``\\emfblip``.
"""

from __future__ import annotations

import contextlib
import itertools
import re
from pathlib import Path

from ..imagesize import probe
from ..types import DocKind, Document, ImageBlock, TextBlock

#: Control words whose group should be skipped wholesale -- metadata,
#: style tables, revision history. Their text is never document content.
_SKIP_DESTINATIONS = frozenset(
    {
        "fonttbl", "colortbl", "stylesheet", "info", "listtable",
        "listoverridetable", "revtbl", "rsidtbl", "generator", "themedata",
        "colorschememapping", "latentstyles", "datastore", "xmlnstbl",
        "upr", "mmathPr", "pntext", "pntxta", "pntxtb", "atrfstart",
        "atrfend", "annotation", "nesttableprops",
    }
)

_BLIP_MIME = {
    "pngblip": "image/png",
    "jpegblip": "image/jpeg",
    "emfblip": "image/x-emf",
    "wmetafile": "image/x-wmf",
    "dibitmap": "image/bmp",
    "wbitmap": "image/bmp",
}

#: ``\u<n>`` unicode escapes, plus the ``\'xx`` codepage byte form.
_UNICODE = re.compile(rb"\\u(-?\d+)\s?\??")
_HEXCHAR = re.compile(rb"\\'([0-9a-fA-F]{2})")


def extract(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse an .rtf file into text and image blocks."""
    path = Path(path)
    doc = Document(path=path, kind=DocKind.RTF)
    raw = path.read_bytes()
    order = itertools.count()

    images, pict_spans = _pictures(raw) if embedded_images else ([], [])
    text = _text(raw, pict_spans)

    if text.strip():
        doc.blocks.append(TextBlock(text.strip(), "paragraph", next(order)))
    for n, (mime, data) in enumerate(images, start=1):
        size = probe(data)
        doc.blocks.append(
            ImageBlock(
                data=data,
                mime=mime,
                name=f"pict{n}{_ext(mime)}",
                order=next(order),
                width=size[0] if size else None,
                height=size[1] if size else None,
            )
        )
    doc.meta["media_count"] = len(images)
    return doc


def _ext(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/bmp": ".bmp",
        "image/x-emf": ".emf",
        "image/x-wmf": ".wmf",
    }.get(mime, ".bin")


def _pictures(raw: bytes) -> tuple[list[tuple[str, bytes]], list[tuple[int, int]]]:
    """Find every ``\\pict`` group and decode its payload.

    Returns the images plus the byte spans they occupied, so the text pass
    can skip them -- hex payloads are megabytes of ``[0-9a-f]`` that would
    otherwise land in the text layer as garbage.
    """
    out: list[tuple[str, bytes]] = []
    spans: list[tuple[int, int]] = []
    pos = 0

    while True:
        start = raw.find(rb"\pict", pos)
        if start == -1:
            break
        # Walk back to the group's opening brace.
        open_brace = raw.rfind(b"{", 0, start)
        if open_brace == -1:
            open_brace = start
        end = _group_end(raw, open_brace)
        chunk = raw[open_brace:end]
        spans.append((open_brace, end))
        pos = end

        mime = next(
            (m for word, m in _BLIP_MIME.items() if b"\\" + word.encode() in chunk),
            None,
        )
        if mime is None:
            mime = "application/octet-stream"

        data = _decode_payload(chunk)
        if data:
            out.append((mime, data))

    return out, spans


def _group_end(raw: bytes, open_brace: int) -> int:
    """Index just past the brace group starting at *open_brace*."""
    depth = 0
    i = open_brace
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == 0x5C:  # backslash escapes the next byte
            i += 2
            continue
        if c == 0x7B:  # {
            depth += 1
        elif c == 0x7D:  # }
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _decode_payload(chunk: bytes) -> bytes:
    """Extract image bytes from a pict group.

    ``\\binN`` marks a raw binary payload; otherwise the data is hex text
    after the last control word.
    """
    binmatch = re.search(rb"\\bin(-?\d+)[ ]?", chunk)
    if binmatch:
        length = int(binmatch.group(1))
        start = binmatch.end()
        if length > 0:
            return chunk[start : start + length]

    # Hex form: take everything after the final control word, keep hex digits.
    tail = chunk
    last = max(tail.rfind(b"\\pict"), 0)
    body = tail[last:]
    # Strip any nested groups (e.g. \\*\\blipuid {...}) before collecting hex.
    body = re.sub(rb"\{[^{}]*\}", b"", body)
    body = re.sub(rb"\\[a-zA-Z]+-?\d*[ ]?", b"", body)
    hexdigits = bytes(c for c in body if c in b"0123456789abcdefABCDEF")
    if len(hexdigits) < 16:
        return b""
    if len(hexdigits) % 2:
        hexdigits = hexdigits[:-1]
    try:
        return bytes.fromhex(hexdigits.decode("ascii"))
    except ValueError:
        return b""


def _text(raw: bytes, skip: list[tuple[int, int]]) -> str:
    """Strip RTF control structure down to readable text."""
    # Blank out picture payloads rather than deleting, so offsets stay valid.
    if skip:
        buf = bytearray(raw)
        for start, end in skip:
            buf[start:end] = b" " * (end - start)
        raw = bytes(buf)

    # Drop whole destination groups that never hold content.
    for word in _SKIP_DESTINATIONS:
        raw = _drop_groups(raw, word.encode())

    out: list[str] = []
    i = 0
    n = len(raw)
    depth = 0

    while i < n:
        c = raw[i]

        if c == 0x7B:  # {
            depth += 1
            i += 1
        elif c == 0x7D:  # }
            depth -= 1
            i += 1
        elif c == 0x5C:  # control word or escape
            m = _UNICODE.match(raw, i)
            if m:
                code = int(m.group(1))
                if code < 0:
                    code += 65536
                with contextlib.suppress(ValueError):
                    out.append(chr(code))
                i = m.end()
                continue
            m = _HEXCHAR.match(raw, i)
            if m:
                out.append(bytes([int(m.group(1), 16)]).decode("cp1252", "replace"))
                i = m.end()
                continue
            word_match = re.match(rb"\\([a-zA-Z]+)(-?\d+)?[ ]?", raw[i:])
            if word_match:
                word = word_match.group(1)
                if word in (b"par", b"line", b"sect", b"page"):
                    out.append("\n")
                elif word in (b"tab", b"cell"):
                    out.append("\t")
                i += word_match.end()
                continue
            # \\{ \\} \\\\ and friends: the escaped byte is literal text.
            if i + 1 < n:
                out.append(chr(raw[i + 1]))
            i += 2
        else:
            out.append(chr(c) if c < 0x80 else bytes([c]).decode("cp1252", "replace"))
            i += 1

    text = "".join(out)
    # Collapse the blank-line storm left by stripped control groups.
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text))


def _drop_groups(raw: bytes, word: bytes) -> bytes:
    """Remove every brace group whose first control word is *word*."""
    out = raw
    needle = b"\\" + word
    pos = 0
    while True:
        idx = out.find(needle, pos)
        if idx == -1:
            return out
        brace = out.rfind(b"{", 0, idx)
        if brace == -1:
            pos = idx + len(needle)
            continue
        # Only drop when the control word really opens the group.
        between = out[brace + 1 : idx].strip()
        if between not in (b"", b"\\*"):
            pos = idx + len(needle)
            continue
        end = _group_end(out, brace)
        out = out[:brace] + b" " + out[end:]
        pos = brace
