"""HTML and plain-text extraction, standard library only.

HTML matters because "print to HTML" and "save email as webpage" are how a
lot of records leave legacy systems, and because images in it arrive three
different ways: a ``data:`` URI, a path next to the file on disk, or a
remote URL. The first two are recoverable; the third is deliberately not
fetched -- a verification pipeline must never reach out to a URL that
arrived inside a user-submitted document.
"""

from __future__ import annotations

import base64
import csv
import io
import itertools
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from ..imagesize import probe
from ..routing import sniff_mime
from ..types import DocKind, Document, ImageBlock, Table, TextBlock

#: Elements whose contents are never shown to a reader.
_INVISIBLE = frozenset({"script", "style", "head", "title", "meta", "noscript"})
_BLOCK = frozenset(
    {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "blockquote", "pre", "table",
    }
)

MAX_TEXT_BYTES = 4_000_000


def extract_html(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse an HTML file into text, tables, and image blocks."""
    path = Path(path)
    doc = Document(path=path, kind=DocKind.HTML)
    raw = path.read_bytes()[:MAX_TEXT_BYTES]
    html = raw.decode(_charset(raw), errors="replace")

    parser = _Harvester(base_dir=path.parent, want_images=embedded_images)
    parser.feed(html)
    parser.close()

    order = itertools.count()
    text = parser.text()
    if text.strip():
        doc.blocks.append(TextBlock(text.strip(), "paragraph", next(order)))

    for rows in parser.tables:
        if rows:
            o = next(order)
            doc.tables.append(Table(rows=rows, order=o))

    for name, data in parser.images:
        size = probe(data)
        doc.blocks.append(
            ImageBlock(
                data=data,
                mime=sniff_mime(data, name),
                name=name,
                order=next(order),
                width=size[0] if size else None,
                height=size[1] if size else None,
            )
        )

    if parser.title:
        doc.meta["title"] = parser.title
    doc.warnings.extend(parser.warnings)
    doc.meta["media_count"] = len(parser.images)
    return doc


def extract_text(
    path: str | Path,
    *,
    embedded_images: bool = True,
    max_pages: int | None = None,
) -> Document:
    """Parse a plain-text or CSV file. CSV also populates ``tables``."""
    path = Path(path)
    doc = Document(path=path, kind=DocKind.TEXT)
    raw = path.read_bytes()[:MAX_TEXT_BYTES]
    text = raw.decode(_charset(raw), errors="replace")

    order = itertools.count()
    if path.suffix.lower() in (".csv", ".tsv"):
        delim = "\t" if path.suffix.lower() == ".tsv" else _sniff_delimiter(text)
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(r)]
        if rows:
            o = next(order)
            doc.tables.append(Table(rows=rows, order=o))
            doc.blocks.append(
                TextBlock(
                    "\n".join(" | ".join(c for c in r) for r in rows), "table", o
                )
            )
            return doc

    if text.strip():
        doc.blocks.append(TextBlock(text.strip(), "paragraph", next(order)))
    return doc


class _Harvester(HTMLParser):
    """Collects visible text, table cells, and recoverable images."""

    def __init__(self, base_dir: Path, want_images: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_dir
        self._want = want_images
        self._skip = 0
        self._chunks: list[str] = []
        self.images: list[tuple[str, bytes]] = []
        self.tables: list[list[list[str]]] = []
        self.warnings: list[str] = []
        self.title: str | None = None
        self._in_title = False
        self._table_stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._seen_src: set[str] = set()

    # -- text ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _INVISIBLE:
            self._skip += 1
            if tag == "title":
                self._in_title = True
            return
        if tag in _BLOCK:
            self._chunks.append("\n")
        if tag == "img" and self._want:
            self._image(dict(attrs))
        elif tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _INVISIBLE:
            self._skip = max(0, self._skip - 1)
            if tag == "title":
                self._in_title = False
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table_stack and any(self._row):
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            rows = self._table_stack.pop()
            if rows:
                self.tables.append(rows)
        if tag in _BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip():
            self.title = data.strip()
            return
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        joined = re.sub(r"[ \t\xa0]+", " ", joined)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)

    # -- images --------------------------------------------------------

    def _image(self, attrs: dict[str, str | None]) -> None:
        src = (attrs.get("src") or "").strip()
        if not src or src in self._seen_src:
            return
        self._seen_src.add(src)

        if src.startswith("data:"):
            data = _decode_data_uri(src)
            if data:
                self.images.append((f"inline{len(self.images) + 1}", data))
            return

        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", src):
            # Never fetch a URL that arrived inside a submitted document --
            # it leaks that the file was opened and invites SSRF.
            self.warnings.append(f"remote image not fetched: {src[:120]}")
            return

        rel = urllib.parse.unquote(src.split("?", 1)[0].split("#", 1)[0])
        candidate = (self._base / rel).resolve()
        try:
            base = self._base.resolve()
            if not candidate.is_relative_to(base):
                self.warnings.append(f"image path escapes the document folder: {src}")
                return
            if candidate.is_file():
                self.images.append((candidate.name, candidate.read_bytes()))
            else:
                self.warnings.append(f"referenced image not found on disk: {src}")
        except OSError as exc:
            self.warnings.append(f"could not read {src}: {exc}")


def _decode_data_uri(uri: str) -> bytes:
    head, _, payload = uri.partition(",")
    if not payload:
        return b""
    try:
        if ";base64" in head:
            return base64.b64decode(payload, validate=False)
        return urllib.parse.unquote_to_bytes(payload)
    except (ValueError, TypeError):
        return b""


def _charset(raw: bytes) -> str:
    """Guess an encoding from a BOM or a meta charset declaration."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    if m:
        name = m.group(1).decode("ascii", "ignore")
        try:
            "x".encode(name)
            return name
        except LookupError:
            pass
    return "utf-8"


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","
