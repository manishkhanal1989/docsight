"""Shared machinery for OPC packages -- the container behind every OOXML
format (.docx, .xlsx, .pptx).

All three are zips with the same bones: content parts under a format
prefix, original image bytes under ``<prefix>/media/``, and relationship
files joining the two. The fiddly parts -- resolving a relationship target
to a package path, sweeping up media the XML never referenced, reading core
properties -- are identical, so they live here once and the per-format
walkers stay small and readable.
"""

from __future__ import annotations

import itertools
import posixpath
import zipfile
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import CorruptDocument
from ..imagesize import probe
from ..routing import sniff_mime
from ..types import ImageBlock

# Namespaces shared across the OOXML formats.
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V = "{urn:schemas-microsoft-com:vml}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCTERMS = "{http://purl.org/dc/terms/}"

#: Tags that reference an image by relationship id, mapped to the attribute
#: holding it. ``a:blip`` is the DrawingML form used by all three formats;
#: ``v:imagedata`` is the legacy VML form still emitted by old templates,
#: scanners and some export tools.
IMAGE_REFS: dict[str, tuple[str, ...]] = {
    f"{A}blip": (f"{R}embed", f"{R}link"),
    f"{V}imagedata": (f"{R}id",),
}


class OpcPackage:
    """A read-only view of an OPC zip, with relationship resolution."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._zf = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise CorruptDocument(
                f"{self.path.name} is not a readable OPC zip: {exc}"
            ) from exc
        self.names: set[str] = set(self._zf.namelist())
        self.consumed: set[str] = set()
        self.warnings: list[str] = []
        self.order = itertools.count()
        self._rels_cache: dict[str, dict[str, str]] = {}

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> OpcPackage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -------------------------------------------------------

    def read(self, part: str) -> bytes:
        return self._zf.read(part)

    def xml(self, part: str) -> ET.Element | None:
        """Parse a part as XML, recording a warning instead of raising."""
        if part not in self.names:
            return None
        try:
            return ET.fromstring(self._zf.read(part))
        except ET.ParseError as exc:
            self.warnings.append(f"{part}: unparseable XML ({exc})")
            return None

    def matching(self, prefix: str, suffix: str = ".xml") -> list[str]:
        """Parts under *prefix*, ordered naturally so slide10 follows slide9."""
        found = [
            n
            for n in self.names
            if n.startswith(prefix) and n.endswith(suffix) and "/_rels/" not in n
        ]
        return sorted(found, key=_natural_key)

    # -- relationships -------------------------------------------------

    def rels(self, part: str) -> dict[str, str]:
        """Map relationship id -> package-absolute target for one part."""
        if part in self._rels_cache:
            return self._rels_cache[part]

        rels_path = posixpath.join(
            posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
        )
        out: dict[str, str] = {}
        root = self.xml(rels_path) if rels_path in self.names else None
        if root is not None:
            base = posixpath.dirname(part)
            for rel in root.findall(f"{PKG_REL}Relationship"):
                rid, target = rel.get("Id"), rel.get("Target")
                if not rid or not target:
                    continue
                if rel.get("TargetMode") == "External":
                    out[rid] = target
                elif target.startswith("/"):
                    out[rid] = target.lstrip("/")
                else:
                    out[rid] = posixpath.normpath(posixpath.join(base, target))
        self._rels_cache[part] = out
        return out

    def related_parts(self, part: str, rel_type_suffix: str) -> list[str]:
        """Targets of one relationship type, e.g. ``"drawing"`` or ``"image"``."""
        rels_path = posixpath.join(
            posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
        )
        root = self.xml(rels_path) if rels_path in self.names else None
        if root is None:
            return []
        base = posixpath.dirname(part)
        out = []
        for rel in root.findall(f"{PKG_REL}Relationship"):
            rtype, target = rel.get("Type", ""), rel.get("Target")
            if not target or not rtype.endswith(rel_type_suffix):
                continue
            out.append(
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join(base, target))
            )
        return out

    # -- images --------------------------------------------------------

    def image(self, rid: str | None, part: str, section: str | None = None) -> ImageBlock | None:
        """Resolve a relationship id in *part* to an image block.

        The block carries a placeholder ``order``; the caller stamps the real
        one after flushing pending text, so a caption keeps its position
        ahead of the image it introduces.
        """
        if not rid:
            return None
        target = self.rels(part).get(rid)
        if target is None:
            self.warnings.append(f"{part}: relationship {rid} has no target")
            return None
        if target not in self.names:
            # An r:link image lives outside the package entirely.
            self.warnings.append(f"{part}: image target not inside package: {target}")
            return None

        data = self._zf.read(target)
        self.consumed.add(target)
        size = probe(data)
        return ImageBlock(
            data=data,
            mime=sniff_mime(data, target),
            name=posixpath.basename(target),
            order=-1,
            width=size[0] if size else None,
            height=size[1] if size else None,
            source="embedded",
            section=section,
        )

    def images_in(self, element: ET.Element, part: str, section: str | None = None) -> Iterator[ImageBlock]:
        """Every image referenced anywhere beneath *element*, in order."""
        for node in element.iter():
            attrs = IMAGE_REFS.get(node.tag)
            if not attrs:
                continue
            rid = next((node.get(a) for a in attrs if node.get(a)), None)
            img = self.image(rid, part, section)
            if img is not None:
                yield img

    def drawing_images(self, part: str, section: str | None = None) -> list[ImageBlock]:
        """Images reached through a part's ``drawing`` relationships.

        Spreadsheets and charts attach pictures this way: the worksheet XML
        only names a drawing part, and the drawing holds the image
        relationships. A walker that reads worksheet XML alone sees nothing.
        """
        out: list[ImageBlock] = []
        for drawing in self.related_parts(part, "/drawing"):
            root = self.xml(drawing)
            if root is None:
                continue
            for img in self.images_in(root, drawing, section):
                out.append(img)
        return out

    def orphan_media(self, prefix: str) -> list[ImageBlock]:
        """Sweep up media parts the XML walk never reached.

        Backstop for shape fills, textboxes, backgrounds, slide layouts and
        parts not traversed. These carry ``referenced=False`` so callers can
        weight them lower -- but dropping them outright loses real scans.
        """
        out: list[ImageBlock] = []
        for name in sorted(n for n in self.names if n.startswith(prefix)):
            if name in self.consumed:
                continue
            data = self._zf.read(name)
            if not data:
                continue
            size = probe(data)
            out.append(
                ImageBlock(
                    data=data,
                    mime=sniff_mime(data, name),
                    name=posixpath.basename(name),
                    order=next(self.order),
                    width=size[0] if size else None,
                    height=size[1] if size else None,
                    source="embedded",
                    referenced=False,
                )
            )
        if out:
            self.warnings.append(
                f"{len(out)} media part(s) unreferenced by the XML walk (shape "
                "fill, textbox, layout or background) -- included with "
                "referenced=False"
            )
        return out

    # -- metadata ------------------------------------------------------

    def core_properties(self) -> dict[str, object]:
        """docProps/core.xml -- author and timestamps, for an audit trail."""
        root = self.xml("docProps/core.xml")
        if root is None:
            return {}
        fields = {
            "title": f"{DC}title",
            "author": f"{DC}creator",
            "subject": f"{DC}subject",
            "last_modified_by": f"{CP}lastModifiedBy",
            "created": f"{DCTERMS}created",
            "modified": f"{DCTERMS}modified",
        }
        meta: dict[str, object] = {}
        for key, tag in fields.items():
            el = root.find(tag)
            if el is not None and el.text:
                meta[key] = el.text
        return meta

    def note_embeddings(self, prefix: str) -> None:
        """Warn about OLE embeddings, which may hide a whole nested file."""
        if any(n.startswith(f"{prefix}/embeddings/") for n in self.names):
            self.warnings.append(
                f"contains OLE embeddings ({prefix}/embeddings/); a nested file "
                "may hold the document you want -- extract and route it separately"
            )


def _natural_key(name: str) -> tuple[object, ...]:
    """Sort ``sheet2.xml`` before ``sheet10.xml``."""
    import re

    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)
    )
