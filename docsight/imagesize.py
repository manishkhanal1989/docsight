"""Pure-stdlib image dimension probing.

Reading width/height is needed to drop seals, logos and signature
scribbles before spending a vision call on them, and we do not want a
Pillow dependency just for that. Pillow is used as a fallback for formats
not parsed here (TIFF, HEIC) when it happens to be installed.
"""

from __future__ import annotations

import struct

_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def probe(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` in pixels, or None if undeterminable."""
    for parser in (_png, _gif, _bmp, _jpeg, _webp):
        try:
            size = parser(data)
        except (struct.error, IndexError, ValueError):
            continue
        if size:
            return size
    return _pillow(data)


def _png(d: bytes) -> tuple[int, int] | None:
    if not d.startswith(b"\x89PNG\r\n\x1a\n") or d[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", d[16:24])
    return w, h


def _gif(d: bytes) -> tuple[int, int] | None:
    if not d.startswith((b"GIF87a", b"GIF89a")):
        return None
    w, h = struct.unpack("<HH", d[6:10])
    return w, h


def _bmp(d: bytes) -> tuple[int, int] | None:
    if not d.startswith(b"BM"):
        return None
    w, h = struct.unpack("<ii", d[18:26])
    return abs(w), abs(h)


def _jpeg(d: bytes) -> tuple[int, int] | None:
    if not d.startswith(b"\xff\xd8"):
        return None
    i = 2
    end = len(d)
    while i < end - 9:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack(">H", d[i + 2 : i + 4])[0]
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", d[i + 5 : i + 9])
            return w, h
        i += 2 + seglen
    return None


def _webp(d: bytes) -> tuple[int, int] | None:
    if not (d.startswith(b"RIFF") and d[8:12] == b"WEBP"):
        return None
    chunk = d[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(d[24:27], "little") + 1
        h = int.from_bytes(d[27:30], "little") + 1
        return w, h
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", d[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(d[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _pillow(data: bytes) -> tuple[int, int] | None:
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None
