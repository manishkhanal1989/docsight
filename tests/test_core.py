"""Tests for docsight.

Run with: python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import docsight  # noqa: E402
from docsight import DocKind, ImageFilter, Strategy  # noqa: E402
from docsight.imagesize import probe  # noqa: E402
from docsight.payload import normalize  # noqa: E402
from tests import fixtures as fx  # noqa: E402

CERT_TEXT = (
    "CERTIFICATE OF MARRIAGE. This certifies that Dana Alvarez and Samira Okonkwo "
    "were joined in marriage on the 14th day of June, 2019, in the County of "
    "Travis, State of Texas, by authority of license number TX-2019-884417, "
    "recorded by the County Clerk."
)


# --- routing --------------------------------------------------------------


def test_detects_docx_by_content_not_extension(tmp_path: Path):
    mislabelled = fx.build_docx(tmp_path / "cert.pdf", fx.para("hello"))
    assert docsight.detect(mislabelled) is DocKind.DOCX


def test_plain_zip_is_not_mistaken_for_docx(tmp_path: Path):
    import zipfile

    z = tmp_path / "bundle.docx"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("notes.txt", "not a word file")
    assert docsight.detect(z) is DocKind.UNKNOWN


def test_image_detected_from_magic_bytes(tmp_path: Path):
    p = tmp_path / "scan.bin"
    p.write_bytes(fx.png(400, 400))
    assert docsight.detect(p) is DocKind.IMAGE


# --- image size probing ---------------------------------------------------


def test_probe_reads_png_dimensions():
    assert probe(fx.png(321, 123)) == (321, 123)


def test_probe_returns_none_for_garbage():
    assert probe(b"not an image at all") is None


# --- docx extraction ------------------------------------------------------


def test_extracts_text_and_table(tmp_path: Path):
    body = fx.para(CERT_TEXT) + fx.table(
        [["Spouse A", "Dana Alvarez"], ["Spouse B", "Samira Okonkwo"]]
    )
    doc = docsight.extract(fx.build_docx(tmp_path / "typed.docx", body))

    assert "Dana Alvarez" in doc.text
    assert len(doc.tables) == 1
    assert dict(doc.tables[0].as_pairs())["Spouse B"] == "Samira Okonkwo"


def test_finds_inline_anchored_and_vml_images(tmp_path: Path):
    """The three ways an image attaches to a Word paragraph, all of which
    occur in documents people actually submit."""
    body = (
        fx.inline_image("rId10")
        + fx.anchored_image("rId11")
        + fx.vml_image("rId12")
    )
    docx = fx.build_docx(
        tmp_path / "three.docx",
        body,
        media={
            "a.png": fx.png(900, 1200),
            "b.png": fx.png(1000, 1300),
            "c.png": fx.png(800, 1100),
        },
        rels={
            "rId10": "media/a.png",
            "rId11": "media/b.png",
            "rId12": "media/c.png",
        },
    )
    doc = docsight.extract(docx)
    assert len(doc.images) == 3
    assert {i.name for i in doc.images} == {"a.png", "b.png", "c.png"}
    assert all(i.referenced for i in doc.images)


def test_image_bytes_are_the_original_bytes(tmp_path: Path):
    """Re-encoding costs OCR accuracy, so embedded extraction must be
    byte-exact."""
    original = fx.png(700, 900)
    docx = fx.build_docx(
        tmp_path / "exact.docx",
        fx.inline_image("rId1"),
        media={"scan.png": original},
        rels={"rId1": "media/scan.png"},
    )
    assert docsight.extract(docx).images[0].data == original


def test_document_order_is_preserved_within_a_paragraph(tmp_path: Path):
    body = fx.caption_then_image("Issued by the County Clerk", "rId1") + fx.para("Seal below")
    doc = docsight.extract(
        fx.build_docx(
            tmp_path / "order.docx",
            body,
            media={"s.png": fx.png(500, 500)},
            rels={"rId1": "media/s.png"},
        )
    )
    seq = [
        "img" if b.is_image else b.text[:9]
        for b in doc.interleaved()
    ]
    assert seq == ["Issued by", "img", "Seal belo"]


def test_tracked_deletions_are_excluded(tmp_path: Path):
    body = fx.para("Spouse: Dana Alvarez") + fx.deleted_para("Spouse: WRONG NAME")
    doc = docsight.extract(fx.build_docx(tmp_path / "tracked.docx", body))
    assert "WRONG NAME" not in doc.text
    assert "Dana Alvarez" in doc.text


def test_orphaned_media_is_recovered_and_flagged(tmp_path: Path):
    """Media with no relationship still has to surface -- a scan dropped into
    a textbox lands here."""
    docx = fx.build_docx(
        tmp_path / "orphan.docx",
        fx.para("See attached."),
        media={"loose.png": fx.png(1000, 1400)},
        rels={},
    )
    doc = docsight.extract(docx)
    assert len(doc.images) == 1
    assert doc.images[0].referenced is False
    assert any("unreferenced" in w for w in doc.warnings)


def test_core_properties_captured(tmp_path: Path):
    doc = docsight.extract(fx.build_docx(tmp_path / "meta.docx", fx.para("x")))
    assert doc.meta["author"] == "County Clerk"
    assert doc.meta["modified"].startswith("2024-03-12")


def test_header_text_excluded_from_body_text(tmp_path: Path):
    """Header boilerplate must not inflate the text-vs-scan decision."""
    header = (
        fx.DOC_OPEN.replace("<w:document", "<w:hdr").replace("<w:body>", "")
        + fx.para("CONFIDENTIAL - BENEFITS INTAKE - DO NOT DISTRIBUTE COPIES")
        + "</w:hdr>"
    )
    docx = fx.build_docx(
        tmp_path / "hdr.docx",
        fx.inline_image("rId1"),
        media={"scan.png": fx.png(900, 1200)},
        rels={"rId1": "media/scan.png"},
        extra_parts={"word/header1.xml": header.encode()},
    )
    doc = docsight.extract(docx)
    assert "CONFIDENTIAL" in doc.text
    assert "CONFIDENTIAL" not in doc.body_text
    assert doc.strategy is Strategy.IMAGE_ONLY


def test_corrupt_docx_raises_corrupt_document(tmp_path: Path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    with pytest.raises((docsight.CorruptDocument, docsight.UnsupportedFormat)):
        docsight.extract(bad)


# --- filtering ------------------------------------------------------------


def test_small_logos_are_filtered_out(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "mixed.docx",
        fx.inline_image("rId1") + fx.inline_image("rId2"),
        media={"seal.png": fx.png(64, 64), "scan.png": fx.png(1100, 1500)},
        rels={"rId1": "media/seal.png", "rId2": "media/scan.png"},
    )
    ready = docsight.prepare(docx, render_fallback=False)
    assert [i.name for i in ready.images] == ["scan.png"]
    assert any(img.name == "seal.png" for img, _ in ready.rejected)


def test_duplicate_images_are_collapsed(tmp_path: Path):
    same = fx.png(900, 1200)
    docx = fx.build_docx(
        tmp_path / "dupe.docx",
        fx.inline_image("rId1") + fx.inline_image("rId2"),
        media={"a.png": same, "b.png": same},
        rels={"rId1": "media/a.png", "rId2": "media/b.png"},
    )
    ready = docsight.prepare(docx, render_fallback=False)
    assert len(ready.images) == 1


def test_keep_all_profile_keeps_everything(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "all.docx",
        fx.inline_image("rId1"),
        media={"tiny.png": fx.png(10, 10)},
        rels={"rId1": "media/tiny.png"},
    )
    ready = docsight.prepare(docx, image_filter=docsight.KEEP_ALL, render_fallback=False)
    assert len(ready.images) == 1


def test_largest_image_comes_first(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "sorted.docx",
        fx.inline_image("rId1") + fx.inline_image("rId2"),
        media={"small.png": fx.png(400, 400), "big.png": fx.png(1200, 1600)},
        rels={"rId1": "media/small.png", "rId2": "media/big.png"},
    )
    ready = docsight.prepare(docx, render_fallback=False)
    assert ready.images[0].name == "big.png"


# --- strategy -------------------------------------------------------------


def test_typed_document_skips_vision(tmp_path: Path):
    doc = docsight.extract(fx.build_docx(tmp_path / "typed.docx", fx.para(CERT_TEXT)))
    assert doc.strategy is Strategy.TEXT_ONLY
    assert docsight.prepare(tmp_path / "typed.docx", render_fallback=False).needs_vision is False


def test_scan_only_document_needs_vision(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "scan.docx",
        fx.anchored_image("rId1"),
        media={"scan.png": fx.png(1200, 1600)},
        rels={"rId1": "media/scan.png"},
    )
    ready = docsight.prepare(docx, render_fallback=False)
    assert ready.strategy is Strategy.IMAGE_ONLY
    assert ready.needs_vision


def test_text_plus_scan_is_hybrid(tmp_path: Path):
    body = fx.para(CERT_TEXT) + fx.inline_image("rId1")
    docx = fx.build_docx(
        tmp_path / "hybrid.docx",
        body,
        media={"scan.png": fx.png(1100, 1500)},
        rels={"rId1": "media/scan.png"},
    )
    ready = docsight.prepare(docx, render_fallback=False)
    assert ready.strategy is Strategy.HYBRID
    assert CERT_TEXT[:20] in ready.text


def test_empty_document(tmp_path: Path):
    doc = docsight.extract(fx.build_docx(tmp_path / "empty.docx", fx.para("")))
    assert doc.strategy is Strategy.EMPTY


def test_weak_text_is_not_treated_as_empty(tmp_path: Path):
    """Text below the trust threshold means 'look at this', not 'reject it'.

    Conflating the two makes the pipeline refuse uploads that plainly
    contain a certificate.
    """
    doc = docsight.extract(
        fx.build_docx(tmp_path / "weak.docx", fx.para("Marriage cert, Travis County."))
    )
    assert doc.strategy is Strategy.IMAGE_ONLY


def test_all_images_filtered_out_still_needs_looking_at(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "tiny_only.docx",
        fx.inline_image("rId1"),
        media={"logo.png": fx.png(40, 40)},
        rels={"rId1": "media/logo.png"},
    )
    ready = docsight.prepare(docx, render_fallback=False)
    assert ready.strategy is Strategy.IMAGE_ONLY
    assert ready.needs_vision
    # An imageless payload for a vision-needing document must never look
    # like a clean result.
    assert not ready.images
    assert any(n.startswith("WARNING") for n in ready.notes)


# --- payload --------------------------------------------------------------


def test_content_is_openai_shaped(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "p.docx",
        fx.inline_image("rId1"),
        media={"scan.png": fx.png(1100, 1500)},
        rels={"rId1": "media/scan.png"},
    )
    content = docsight.prepare(docx, render_fallback=False).to_content("Extract names.")
    assert content[0] == {"type": "text", "text": "Extract names."}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/")


def test_content_is_anthropic_shaped(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "p2.docx",
        fx.inline_image("rId1"),
        media={"scan.png": fx.png(1100, 1500)},
        rels={"rId1": "media/scan.png"},
    )
    content = docsight.prepare(docx, render_fallback=False).to_content(
        "Extract names.", dialect="anthropic"
    )
    part = content[1]
    assert part["type"] == "image"
    assert part["source"]["type"] == "base64"
    assert part["source"]["media_type"].startswith("image/")


def test_text_layer_is_appended_for_cross_checking(tmp_path: Path):
    body = fx.para(CERT_TEXT) + fx.inline_image("rId1")
    docx = fx.build_docx(
        tmp_path / "x.docx",
        body,
        media={"scan.png": fx.png(1100, 1500)},
        rels={"rId1": "media/scan.png"},
    )
    content = docsight.prepare(docx, render_fallback=False).to_content("Verify.")
    assert any("cross-checking" in p.get("text", "") for p in content)


def test_oversized_image_is_downscaled():
    pytest.importorskip("PIL", reason="downscaling needs Pillow")
    big = docsight.ImageBlock(data=fx.png(3000, 3000), mime="image/png", name="big.png")
    small = normalize(big, max_edge=1200)
    assert small is not None
    assert max(small.width, small.height) == 1200
    assert small.nbytes < big.nbytes


def test_api_safe_image_passes_through_untouched():
    img = docsight.ImageBlock(
        data=fx.png(800, 600), mime="image/png", name="ok.png", width=800, height=600
    )
    assert normalize(img, max_edge=1600) is img


def test_metafile_is_reported_unusable_not_silently_sent(tmp_path: Path):
    """EMF is the quiet killer: no vision API accepts it, so it must surface
    rather than vanish."""
    docx = fx.build_docx(
        tmp_path / "emf.docx",
        fx.inline_image("rId1"),
        media={"paste.emf": fx.emf(40_000)},
        rels={"rId1": "media/paste.emf"},
    )
    doc = docsight.extract(docx)
    assert doc.images[0].mime == "image/x-emf"

    ready = docsight.prepare(docx, image_filter=ImageFilter(keep_unsized=True),
                            render_fallback=False)
    content = ready.to_content("Read it.")
    assert not any(p["type"] in ("image_url", "image") for p in content)
    assert any("not API-consumable" in n for n in ready.notes)


# --- bare images ----------------------------------------------------------


def test_bare_png_becomes_single_image_document(tmp_path: Path):
    p = tmp_path / "phone.png"
    p.write_bytes(fx.png(1400, 1900))
    ready = docsight.prepare(p, render_fallback=False)
    assert ready.document.kind is DocKind.IMAGE
    assert ready.strategy is Strategy.IMAGE_ONLY
    assert len(ready.images) == 1


# --- extension point ------------------------------------------------------


def test_backends_registry_is_overridable(tmp_path: Path):
    from docsight.api import BACKENDS, _load_backends

    _load_backends()
    original = BACKENDS[DocKind.IMAGE]
    sentinel = docsight.Document(path=tmp_path / "x", kind=DocKind.IMAGE)
    BACKENDS[DocKind.IMAGE] = lambda path, **kw: sentinel
    try:
        p = tmp_path / "img.png"
        p.write_bytes(fx.png(300, 300))
        assert docsight.extract(p) is sentinel
    finally:
        BACKENDS[DocKind.IMAGE] = original
