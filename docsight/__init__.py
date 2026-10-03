"""docsight -- get text and images out of real-world documents and into a
vision model.

Built for document-verification pipelines, where the same evidence arrives
as a .docx with a scan pasted in, a born-digital PDF, a legacy .doc, or a
phone photo -- and the pipeline has to handle all of them the same way.

    import docsight

    ready = docsight.prepare("marriage_certificate.docx")
    if ready.needs_vision:
        content = ready.to_content("Extract both spouse names and the date.")
        # -> OpenAI-compatible message content, ready for Qwen-VL et al.
    else:
        print(ready.text)   # text layer was trustworthy; no vision call spent

The .docx path needs no third-party packages at all. PDF needs PyMuPDF;
legacy .doc and faithful page rendering need LibreOffice installed.
"""

from __future__ import annotations

from .api import BACKENDS, Prepared, extract, prepare, prepare_all, supported_kinds
from .errors import (
    ConversionFailed,
    CorruptDocument,
    DocSightError,
    MissingDependency,
    UnsupportedFormat,
)
from .filters import KEEP_ALL, STRICT, ImageFilter
from .llm import Reading, build_prompt, parse_json, read, read_all
from .payload import build_content, data_url, image_part, normalize
from .render import can_render, render_pages
from .routing import detect, sniff_mime, unreadable_reason
from .strategy import decide, explain
from .types import (
    Block,
    DocKind,
    Document,
    ImageBlock,
    Strategy,
    Table,
    TextBlock,
)

__version__ = "0.1.0"

__all__ = [
    # main entry points
    "extract",
    "prepare",
    "prepare_all",
    "Prepared",
    "supported_kinds",
    # types
    "Document",
    "DocKind",
    "Strategy",
    "Block",
    "TextBlock",
    "ImageBlock",
    "Table",
    # filtering and strategy
    "ImageFilter",
    "KEEP_ALL",
    "STRICT",
    "decide",
    "explain",
    # rendering
    "render_pages",
    "can_render",
    # payload building
    "build_content",
    "image_part",
    "normalize",
    "data_url",
    # routing
    "detect",
    "sniff_mime",
    "unreadable_reason",
    # vision-model calls
    "read",
    "read_all",
    "Reading",
    "build_prompt",
    "parse_json",
    # errors
    "DocSightError",
    "UnsupportedFormat",
    "CorruptDocument",
    "MissingDependency",
    "ConversionFailed",
    # extension point
    "BACKENDS",
    "__version__",
]
