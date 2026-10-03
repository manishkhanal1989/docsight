"""Command line inspector: ``python -m docsight <files...>``

Exists because the first question about any new document is always "what
did the extractor actually see in there?"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import KEEP_ALL, STRICT, DocSightError, ImageFilter, prepare_all, supported_kinds
from .backends import office


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m docsight",
        description="Inspect what docsight extracts from a document.",
        epilog="Formats: " + ", ".join(k.value for k in supported_kinds()),
    )
    ap.add_argument("paths", nargs="*", type=Path, help="files to inspect")
    ap.add_argument("--dump", type=Path, metavar="DIR", help="write kept images into DIR")
    ap.add_argument(
        "--filter",
        choices=("default", "strict", "all"),
        default="default",
        help="image filtering profile (default: default)",
    )
    ap.add_argument("--dpi", type=int, default=250, help="page render DPI (default: 250)")
    ap.add_argument("--no-render", action="store_true", help="disable page-render fallback")
    ap.add_argument("--text", action="store_true", help="print the extracted text layer")
    ap.add_argument("--formats", action="store_true", help="list supported formats and exit")
    args = ap.parse_args(argv)

    if args.formats:
        print("Supported formats:")
        for kind in supported_kinds():
            print(f"  {kind.value:<9} {kind.family}")
        print("\nLibreOffice:", "found" if office.available() else "NOT found")
        return 0

    if not args.paths:
        ap.error("give at least one file, or --formats")

    flt: ImageFilter = {"default": ImageFilter(), "strict": STRICT, "all": KEEP_ALL}[
        args.filter
    ]

    if not office.available():
        print(
            "note: LibreOffice not found -- legacy .doc/.xls/.ppt and Office page "
            "rendering are unavailable in this environment\n",
            file=sys.stderr,
        )

    failures = 0
    for path in args.paths:
        print(f"=== {path}")
        try:
            results = prepare_all(
                path,
                image_filter=flt,
                render_fallback=not args.no_render,
                render_dpi=args.dpi,
            )
        except (DocSightError, FileNotFoundError) as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        if len(results) > 1:
            print(f"  container holding {len(results)} document(s)\n")

        for ready in results:
            _report(ready, indent="  " if len(results) == 1 else "    ", args=args)
            if args.dump:
                out = args.dump / path.stem / Path(ready.name).stem
                written = [
                    img.save(out, stem=f"{i:03d}-{Path(img.name).stem}")
                    for i, img in enumerate(ready.images)
                ]
                print(f"  wrote      {len(written)} image(s) to {out}")
            if len(results) > 1:
                print()

    return 1 if failures else 0


def _report(ready, indent: str, args) -> None:
    doc = ready.document
    p = lambda label, value: print(f"{indent}{label:<13}{value}")  # noqa: E731

    if ready.name != doc.path.name:
        p("member", ready.name)
    p("kind", doc.kind.value)
    p("strategy", ready.strategy.value)
    p("text", f"{len(ready.text)} chars")
    p("tables", len(doc.tables))
    p("images", f"{len(ready.images)} kept, {len(ready.rejected)} filtered")

    for img in ready.images:
        dims = f"{img.width}x{img.height}" if img.width else "?x?"
        flags = "" if img.referenced else " [unreferenced]"
        where = f" {img.section}" if img.section else ""
        print(
            f"{indent}  + {img.name:<20} {img.mime:<14} {dims:>11} "
            f"{img.nbytes / 1024:>8.1f} KB  {img.source}{flags}{where}"
        )
    for img, reason in ready.rejected:
        print(f"{indent}  - {img.name:<20} {reason}")

    for key in ("author", "subject", "created", "modified", "page_count",
                "sheet_names", "slide_count", "attachment_count", "member_count"):
        if key in doc.meta:
            p(key, doc.meta[key])
    for note in ready.notes:
        p("note", note)
    for warn in doc.warnings:
        p("warning", warn)

    if args.text and ready.text:
        print(f"{indent}--- text ---")
        for line in ready.text.splitlines():
            print(f"{indent}{line}")


if __name__ == "__main__":
    raise SystemExit(main())
