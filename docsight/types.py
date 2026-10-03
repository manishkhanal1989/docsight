"""Core data types for extracted documents."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class DocKind(str, Enum):
    """Recognised input formats."""

    # OOXML
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    # legacy OLE2 Office
    DOC = "doc"
    XLS = "xls"
    PPT = "ppt"
    # OpenDocument
    ODT = "odt"
    ODS = "ods"
    ODP = "odp"
    # other documents
    PDF = "pdf"
    RTF = "rtf"
    HTML = "html"
    TEXT = "text"
    IMAGE = "image"
    # containers, which hold other documents
    EMAIL = "email"
    ARCHIVE = "archive"
    UNKNOWN = "unknown"

    @property
    def is_container(self) -> bool:
        return self in (DocKind.EMAIL, DocKind.ARCHIVE)

    @property
    def can_hold_images(self) -> bool:
        """Whether this format can carry images at all.

        Plain text and CSV cannot, so for them "the text is weak, go look at
        the pixels" is not an available move -- there are no pixels, and
        never will be.
        """
        return self is not DocKind.TEXT

    @property
    def family(self) -> str:
        """Coarse grouping, for choosing a prompt or a text threshold."""
        return {
            DocKind.DOCX: "word",
            DocKind.DOC: "word",
            DocKind.ODT: "word",
            DocKind.RTF: "word",
            DocKind.XLSX: "sheet",
            DocKind.XLS: "sheet",
            DocKind.ODS: "sheet",
            DocKind.PPTX: "slides",
            DocKind.PPT: "slides",
            DocKind.ODP: "slides",
            DocKind.PDF: "paged",
            DocKind.IMAGE: "image",
            DocKind.HTML: "markup",
            DocKind.TEXT: "markup",
            DocKind.EMAIL: "container",
            DocKind.ARCHIVE: "container",
        }.get(self, "unknown")


class Strategy(str, Enum):
    """How a document should be handed to a vision model.

    TEXT_ONLY   the text layer is trustworthy; skip the vision call
    IMAGE_ONLY  a scan with no usable text; send the image bytes
    HYBRID      both exist; send pixels and use the text as a cross-check
    EMPTY       nothing extractable
    """

    TEXT_ONLY = "text_only"
    IMAGE_ONLY = "image_only"
    HYBRID = "hybrid"
    EMPTY = "empty"


TextRole = Literal[
    "paragraph",  # body prose
    "table",  # a rendered table
    "header",  # page header (boilerplate)
    "footer",  # page footer (boilerplate)
    "page",  # one PDF page
    "sheet",  # one spreadsheet worksheet
    "slide",  # one presentation slide
    "notes",  # presenter notes
    "subject",  # email subject / message headers
]
ImageSource = Literal["embedded", "rendered"]

#: Roles that carry boilerplate rather than content. Excluded from
#: ``body_text`` so they cannot inflate the text-vs-scan decision.
BOILERPLATE_ROLES: frozenset[str] = frozenset({"header", "footer"})


@dataclass(frozen=True)
class TextBlock:
    """A run of text, in document order."""

    text: str
    role: TextRole = "paragraph"
    order: int = 0
    page: int | None = None
    #: Worksheet name, slide number, attachment filename -- whatever names
    #: the subdivision this block came from.
    section: str | None = None

    @property
    def is_image(self) -> bool:
        return False


@dataclass(frozen=True)
class ImageBlock:
    """An image, in document order.

    ``data`` is the original bytes as stored in the container -- never
    re-encoded -- unless ``source`` is ``"rendered"``.
    """

    data: bytes
    mime: str
    name: str = ""
    order: int = 0
    page: int | None = None
    width: int | None = None
    height: int | None = None
    source: ImageSource = "embedded"
    referenced: bool = True
    section: str | None = None

    @property
    def is_image(self) -> bool:
        return True

    @property
    def nbytes(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def pixels(self) -> int | None:
        if self.width and self.height:
            return self.width * self.height
        return None

    @property
    def ext(self) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
            "image/webp": ".webp",
            "image/heic": ".heic",
            "image/x-emf": ".emf",
            "image/x-wmf": ".wmf",
            "image/svg+xml": ".svg",
        }.get(self.mime, ".bin")

    def save(self, directory: str | Path, stem: str | None = None) -> Path:
        """Write the bytes to *directory*; returns the path written."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"{stem or f'img{self.order:04d}'}{self.ext}"
        out.write_bytes(self.data)
        return out


Block = TextBlock | ImageBlock


@dataclass(frozen=True)
class Table:
    """Tabular cell text, for structured field lookup.

    Covers a Word table, a worksheet region, and an HTML ``<table>`` alike.
    """

    rows: list[list[str]]
    order: int = 0
    section: str | None = None

    def cells(self) -> Iterator[str]:
        for row in self.rows:
            yield from row

    def as_pairs(self) -> list[tuple[str, str]]:
        """Two-column rows read as label/value pairs -- the common
        certificate and form layout."""
        return [(r[0].strip(), r[1].strip()) for r in self.rows if len(r) == 2]

    def lookup(self, label: str, default: str = "") -> str:
        """Find a value by its label, case- and punctuation-insensitively."""
        want = _norm(label)
        for row in self.rows:
            if len(row) >= 2 and _norm(row[0]) == want:
                return row[1].strip()
        return default


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


@dataclass
class Document:
    """The result of :func:`docsight.extract`.

    A container input (email, archive) carries its contents in
    :attr:`children` rather than flattening them, so a bundle of three
    attachments stays three distinguishable documents.
    """

    path: Path
    kind: DocKind
    blocks: list[Block] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    children: list[Document] = field(default_factory=list)
    #: For a child document, how it was named inside its container.
    origin: str | None = None

    # -- views ---------------------------------------------------------

    @property
    def texts(self) -> list[TextBlock]:
        return [b for b in self.blocks if isinstance(b, TextBlock)]

    @property
    def images(self) -> list[ImageBlock]:
        return [b for b in self.blocks if isinstance(b, ImageBlock)]

    @property
    def text(self) -> str:
        """All of this document's own text, in order."""
        return "\n".join(b.text for b in self.texts if b.text.strip())

    @property
    def body_text(self) -> str:
        """Content text only -- headers and footers excluded, since
        boilerplate there inflates the text-vs-scan decision."""
        return "\n".join(
            b.text
            for b in self.texts
            if b.role not in BOILERPLATE_ROLES and b.text.strip()
        )

    @property
    def strategy(self) -> Strategy:
        from .strategy import decide

        return decide(self)

    def interleaved(self) -> Iterator[Block]:
        """This document's blocks in document order."""
        return iter(sorted(self.blocks, key=lambda b: b.order))

    # -- containers ----------------------------------------------------

    @property
    def is_container(self) -> bool:
        return bool(self.children)

    def flatten(self) -> list[Document]:
        """This document followed by every descendant, depth-first."""
        out = [self]
        for child in self.children:
            out.extend(child.flatten())
        return out

    def leaves(self) -> list[Document]:
        """Every document that holds content rather than other documents."""
        return [d for d in self.flatten() if not d.children]

    @property
    def all_images(self) -> list[ImageBlock]:
        """Images from this document and every descendant."""
        return [img for d in self.flatten() for img in d.images]

    @property
    def all_body_text(self) -> str:
        """Body text from this document and every descendant."""
        parts = []
        for d in self.flatten():
            text = d.body_text
            if text.strip():
                label = d.origin or d.path.name
                parts.append(f"--- {label} ---\n{text}" if d.origin else text)
        return "\n\n".join(parts)

    # -- actions -------------------------------------------------------

    def render_pages(self, dpi: int = 250, max_pages: int | None = None) -> list[ImageBlock]:
        """Rasterise the document as it *looks*, one image per page.

        Needs PyMuPDF, plus LibreOffice for anything but PDF and images.
        Raises :class:`docsight.errors.MissingDependency` when unavailable.
        """
        from .render import render_pages

        return render_pages(self.path, self.kind, dpi=dpi, max_pages=max_pages)

    def save_images(self, directory: str | Path) -> list[Path]:
        return [
            img.save(directory, stem=f"{self.path.stem}-{i:03d}")
            for i, img in enumerate(self.images)
        ]

    def summary(self) -> str:
        imgs = self.images
        px = ", ".join(f"{i.width}x{i.height}" if i.width else "?" for i in imgs[:6])
        kids = f" +{len(self.children)} attached" if self.children else ""
        return (
            f"{self.path.name} [{self.kind.value}] "
            f"strategy={self.strategy.value} "
            f"text={len(self.body_text)}ch "
            f"images={len(imgs)}" + (f" ({px})" if imgs else "") + kids
        )
