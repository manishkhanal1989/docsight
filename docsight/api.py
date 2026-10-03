"""The front door: :func:`extract`, :func:`prepare`, :func:`prepare_all`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import UnsupportedFormat
from .filters import ImageFilter, apply
from .payload import DEFAULT_MAX_EDGE, Dialect, build_content
from .routing import detect, unreadable_reason
from .strategy import MIN_TEXT_CHARS, decide, explain
from .types import DocKind, Document, ImageBlock, Strategy

#: Format -> backend. Replace or extend an entry to add a format:
#: ``docsight.BACKENDS[DocKind.PDF] = my_extractor``
BACKENDS: dict[DocKind, Callable[..., Document]] = {}

#: Backends that only take ``include_headers``, not the paged options.
_WORD_LIKE = frozenset({DocKind.DOCX})
#: Backends taking no options beyond the path.
_SIMPLE = frozenset({DocKind.IMAGE})
#: Backends that recurse and so need the depth guard threaded through.
_CONTAINERS = frozenset({DocKind.EMAIL, DocKind.ARCHIVE})

_loaded = False


def _load_backends() -> None:
    global _loaded
    if _loaded:
        return
    from .backends import container, image, markup, odf, office, pdf, pptx, rtf, xlsx
    from .backends import docx as docx_mod

    defaults: dict[DocKind, Callable[..., Document]] = {
        DocKind.DOCX: docx_mod.extract,
        DocKind.XLSX: xlsx.extract,
        DocKind.PPTX: pptx.extract,
        DocKind.PDF: pdf.extract,
        DocKind.IMAGE: image.extract,
        DocKind.RTF: rtf.extract,
        DocKind.HTML: markup.extract_html,
        DocKind.TEXT: markup.extract_text,
        DocKind.ODT: odf.extract,
        DocKind.ODS: odf.extract,
        DocKind.ODP: odf.extract,
        DocKind.EMAIL: container.extract_email,
        DocKind.ARCHIVE: container.extract_archive,
        # Legacy OLE2 formats have no pure-Python reader; all three route
        # through LibreOffice to PDF.
        DocKind.DOC: office.extract,
        DocKind.XLS: office.extract,
        DocKind.PPT: office.extract,
    }
    for kind, fn in defaults.items():
        BACKENDS.setdefault(kind, fn)
    _loaded = True


def supported_kinds() -> list[DocKind]:
    """Every format with a registered backend."""
    _load_backends()
    return sorted(BACKENDS, key=lambda k: k.value)


def extract(
    path: str | Path,
    *,
    include_headers: bool = True,
    embedded_images: bool = True,
    max_pages: int | None = None,
    _depth: int = 0,
) -> Document:
    """Extract interleaved text and images from a document.

    Routes on file content, not extension, so a ``.docx`` that is really a
    PDF still parses. A container (email, archive) returns a document whose
    ``children`` hold its members.

    Raises :class:`~docsight.errors.UnsupportedFormat` for formats with no
    backend, and :class:`~docsight.errors.MissingDependency` when a backend
    needs something that is not installed.
    """
    _load_backends()
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    kind = detect(path)
    backend = BACKENDS.get(kind)
    if backend is None:
        reason = unreadable_reason(path)
        raise UnsupportedFormat(
            f"{path.name}: {reason}"
            if reason
            else f"{path.name}: detected {kind.value}, which has no registered backend"
        )

    if kind in _SIMPLE:
        return backend(path)
    if kind in _WORD_LIKE:
        return backend(path, include_headers=include_headers)
    if kind in _CONTAINERS:
        return backend(
            path,
            embedded_images=embedded_images,
            max_pages=max_pages,
            _depth=_depth,
        )
    return backend(path, embedded_images=embedded_images, max_pages=max_pages)


@dataclass
class Prepared:
    """A document resolved into something ready to send to a vision model."""

    document: Document
    strategy: Strategy
    images: list[ImageBlock] = field(default_factory=list)
    text: str = ""
    rejected: list[tuple[ImageBlock, str]] = field(default_factory=list)
    rendered: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def needs_vision(self) -> bool:
        return self.strategy in (Strategy.IMAGE_ONLY, Strategy.HYBRID)

    @property
    def kind(self) -> DocKind:
        return self.document.kind

    @property
    def name(self) -> str:
        """How to refer to this document -- its name inside its container
        when it came from one, otherwise its filename."""
        return self.document.origin or self.document.path.name

    def to_content(
        self,
        prompt: str,
        *,
        dialect: Dialect = "openai",
        include_text_layer: bool = True,
        max_edge: int | None = DEFAULT_MAX_EDGE,
    ) -> list[dict[str, Any]]:
        """Build a chat message ``content`` list for this document."""
        content, unusable = build_content(
            self.images,
            prompt=prompt,
            text_layer=self.text if include_text_layer else None,
            dialect=dialect,
            max_edge=max_edge,
        )
        for img in unusable:
            self.notes.append(
                f"{img.name} ({img.mime}) is not API-consumable; "
                "render the page instead"
            )
        return content

    def summary(self) -> str:
        return (
            f"{self.name} [{self.kind.value}]: {self.strategy.value}, "
            f"{len(self.images)} image(s)"
            f"{' (page renders)' if self.rendered else ''}, "
            f"{len(self.text)} text chars"
        )


def prepare(
    path: str | Path,
    *,
    image_filter: ImageFilter | None = None,
    min_text_chars: int = MIN_TEXT_CHARS,
    render_fallback: bool = True,
    render_dpi: int = 250,
    max_pages: int | None = None,
    include_headers: bool = True,
) -> Prepared:
    """Extract, filter, and decide in one call.

    This is the function a verification pipeline should call for a single
    document. It yields the images actually worth sending and the text worth
    cross-checking against.

    For a container, this treats the whole bundle as one document -- useful
    for "find the certificate in here". Use :func:`prepare_all` when each
    member needs its own answer.

    With *render_fallback* on, a document whose embedded images are all
    filtered out or unusable -- a typed certificate, or one pasted as an EMF
    metafile -- falls back to page renders, so nothing silently produces an
    empty payload. That path needs LibreOffice for anything but PDF and
    images; when it is absent the reason is recorded in ``notes`` rather
    than raised.
    """
    doc = extract(
        path,
        include_headers=include_headers,
        embedded_images=True,
        max_pages=max_pages,
    )
    return _prepare_document(
        doc,
        image_filter=image_filter,
        min_text_chars=min_text_chars,
        render_fallback=render_fallback,
        render_dpi=render_dpi,
        max_pages=max_pages,
        union=doc.is_container,
    )


def prepare_all(
    path: str | Path,
    *,
    image_filter: ImageFilter | None = None,
    min_text_chars: int = MIN_TEXT_CHARS,
    render_fallback: bool = True,
    render_dpi: int = 250,
    max_pages: int | None = None,
    include_headers: bool = True,
) -> list[Prepared]:
    """Like :func:`prepare`, but one result per document in a container.

    Always returns a list: length one for an ordinary file, one entry per
    member for an email or archive. Each entry carries its own strategy, so
    a bundle holding a typed form and a scanned certificate spends a vision
    call on exactly one of them.
    """
    doc = extract(
        path,
        include_headers=include_headers,
        embedded_images=True,
        max_pages=max_pages,
    )
    targets = doc.leaves() if doc.is_container else [doc]
    out = []
    for target in targets:
        if target.kind.is_container and not target.blocks:
            continue  # an empty nested container contributes nothing
        out.append(
            _prepare_document(
                target,
                image_filter=image_filter,
                min_text_chars=min_text_chars,
                render_fallback=render_fallback,
                render_dpi=render_dpi,
                max_pages=max_pages,
                union=False,
            )
        )
    return out


def _prepare_document(
    doc: Document,
    *,
    image_filter: ImageFilter | None,
    min_text_chars: int,
    render_fallback: bool,
    render_dpi: int,
    max_pages: int | None,
    union: bool,
) -> Prepared:
    """Filter and classify one already-extracted document.

    *union* folds in descendants, for treating a container as a whole.
    """
    flt = image_filter or ImageFilter()
    images = doc.all_images if union else doc.images
    text = doc.all_body_text if union else doc.body_text

    kept, rejected = apply(images, flt)
    strategy = decide(
        doc, min_text_chars=min_text_chars, image_filter=flt, union=union
    )

    out = Prepared(
        document=doc,
        strategy=strategy,
        images=kept,
        text=text,
        rejected=rejected,
        notes=[
            explain(doc, min_text_chars=min_text_chars, image_filter=flt, union=union)
        ],
    )

    if not kept and strategy != Strategy.TEXT_ONLY:
        if render_fallback and not doc.is_container:
            from .render import render_pages

            try:
                pages = render_pages(
                    doc.path, doc.kind, dpi=render_dpi, max_pages=max_pages
                )
            except Exception as exc:
                # A missing LibreOffice or a conversion failure must not sink
                # the whole extraction -- the text layer may still be enough.
                out.notes.append(f"page rendering unavailable: {exc}")
            else:
                out.images = pages
                out.rendered = True
                out.strategy = Strategy.HYBRID if out.text else Strategy.IMAGE_ONLY
                out.notes.append(
                    f"fell back to {len(pages)} page render(s) at {render_dpi} dpi"
                )
        elif doc.is_container:
            out.notes.append(
                "containers are not rendered; use prepare_all() to handle each "
                "member on its own"
            )
        else:
            out.notes.append("no sendable image and render_fallback is off")

    # Never hand back a vision-needing document with an imageless payload and
    # no reason why -- that is how a pipeline silently "verifies" a document
    # it never actually saw.
    if out.needs_vision and not out.images:
        out.notes.append(
            "WARNING: this document needs vision but has no sendable image; "
            "treat it as unverifiable rather than as a clean result"
        )

    return out
