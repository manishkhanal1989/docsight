"""Tests for the non-Word formats, containers, and the LLM layer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import docsight  # noqa: E402
from docsight import DocKind, Strategy  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_more as fm  # noqa: E402

CERT_ROWS = [
    ["Field", "Value"],
    ["Spouse A", "Dana Alvarez"],
    ["Spouse B", "Samira Okonkwo"],
    ["Date of marriage", "14 June 2019"],
    ["County", "Travis"],
    ["License number", "TX-2019-884417"],
]


# --- xlsx -----------------------------------------------------------------


def test_xlsx_reads_shared_strings(tmp_path: Path):
    """Cell text is indirected through a string table; reading sheet XML
    alone would yield indices."""
    p = fm.build_xlsx(tmp_path / "cert.xlsx", {"Marriage": CERT_ROWS})
    doc = docsight.extract(p)

    assert doc.kind is DocKind.XLSX
    assert "Samira Okonkwo" in doc.text
    assert doc.tables[0].lookup("License number") == "TX-2019-884417"


def test_xlsx_finds_image_behind_a_drawing_part(tmp_path: Path):
    """Excel never references pictures from worksheet XML -- only through a
    drawing part. This is the usual reason a scan in an xlsx goes missing."""
    p = fm.build_xlsx(
        tmp_path / "scan.xlsx",
        {"Sheet1": [["See attached scan"]]},
        image=fm.scan_png(),
        image_on="Sheet1",
    )
    doc = docsight.extract(p)
    assert len(doc.images) == 1
    assert doc.images[0].referenced is True
    assert doc.images[0].section == "Sheet1"


def test_xlsx_sheet_order_and_names_come_from_the_workbook(tmp_path: Path):
    p = fm.build_xlsx(
        tmp_path / "multi.xlsx",
        {"Summary": [["a"]], "Marriage": [["b"]], "Children": [["c"]]},
    )
    doc = docsight.extract(p)
    assert doc.meta["sheet_names"] == ["Summary", "Marriage", "Children"]


def test_xlsx_sheet_cap(tmp_path: Path):
    p = fm.build_xlsx(
        tmp_path / "many.xlsx", {f"S{i}": [[f"v{i}"]] for i in range(5)}
    )
    doc = docsight.extract(p, max_pages=2)
    assert doc.meta["sheet_count"] == 2


# --- pptx -----------------------------------------------------------------


def test_pptx_text_and_images_per_slide(tmp_path: Path):
    p = fm.build_pptx(
        tmp_path / "deck.pptx",
        [
            (["Marriage Certificate", "Page 1 of 2"], fm.scan_png(1)),
            (["Page 2 of 2"], fm.scan_png(2)),
        ],
        notes={1: "Scanned at the county office"},
    )
    doc = docsight.extract(p)

    assert doc.kind is DocKind.PPTX
    assert doc.meta["slide_count"] == 2
    assert len(doc.images) == 2
    assert [i.page for i in doc.images] == [1, 2]
    assert "Marriage Certificate" in doc.text


def test_pptx_keeps_paragraphs_on_separate_lines(tmp_path: Path):
    """A naive join of every a:t run would merge a title into its bullets."""
    p = fm.build_pptx(tmp_path / "lines.pptx", [(["Title", "Bullet one"], None)])
    doc = docsight.extract(p)
    assert "Title\nBullet one" in doc.text


def test_pptx_notes_are_marked_not_body(tmp_path: Path):
    p = fm.build_pptx(
        tmp_path / "notes.pptx",
        [(["Slide text"], None)],
        notes={1: "Internal remark"},
    )
    doc = docsight.extract(p)
    roles = {b.role for b in doc.texts}
    assert "notes" in roles
    assert any("Internal remark" in b.text for b in doc.texts if b.role == "notes")


def test_deck_of_scans_routes_to_vision(tmp_path: Path):
    """Slides carry page furniture that would clear the prose threshold, so
    the slides family uses a higher one."""
    p = fm.build_pptx(
        tmp_path / "scans.pptx",
        [([f"Page {i} of 3"], fm.scan_png(i)) for i in range(1, 4)],
    )
    ready = docsight.prepare(p, render_fallback=False)
    assert ready.strategy is Strategy.IMAGE_ONLY
    assert len(ready.images) == 3


# --- odf ------------------------------------------------------------------


def test_odt_text_table_and_image(tmp_path: Path):
    p = fm.build_odt(
        tmp_path / "cert.odt",
        ["CERTIFICATE OF MARRIAGE", "Travis County, Texas"],
        rows=CERT_ROWS,
        image=fm.scan_png(),
    )
    doc = docsight.extract(p)

    assert doc.kind is DocKind.ODT
    assert "CERTIFICATE OF MARRIAGE" in doc.text
    assert doc.tables[0].lookup("Spouse A") == "Dana Alvarez"
    assert len(doc.images) == 1
    assert doc.meta["title"] == "ODF Certificate"


def test_ods_detected_from_mimetype_member(tmp_path: Path):
    p = fm.build_odt(
        tmp_path / "sheet.ods",
        [],
        rows=CERT_ROWS,
        mimetype="application/vnd.oasis.opendocument.spreadsheet",
    )
    assert docsight.detect(p) is DocKind.ODS
    assert docsight.extract(p).kind is DocKind.ODS


def test_odp_detected_from_mimetype_member(tmp_path: Path):
    p = fm.build_odt(
        tmp_path / "deck.odp",
        ["Slide one"],
        mimetype="application/vnd.oasis.opendocument.presentation",
    )
    assert docsight.detect(p) is DocKind.ODP


# --- rtf ------------------------------------------------------------------


def test_rtf_text_excludes_control_tables(tmp_path: Path):
    """Font, colour and info groups must not leak into the text layer."""
    p = fm.build_rtf(tmp_path / "cert.rtf", "CERTIFICATE OF MARRIAGE\nDana Alvarez")
    doc = docsight.extract(p)

    assert doc.kind is DocKind.RTF
    assert "CERTIFICATE OF MARRIAGE" in doc.text
    assert "Dana Alvarez" in doc.text
    assert "Calibri" not in doc.text
    assert "fonttbl" not in doc.text
    assert "Registry" not in doc.text  # the \info group


def test_rtf_decodes_hex_encoded_image(tmp_path: Path):
    original = fm.scan_png()
    p = fm.build_rtf(tmp_path / "scan.rtf", "Scan below", image=original)
    doc = docsight.extract(p)

    assert len(doc.images) == 1
    assert doc.images[0].mime == "image/png"
    assert doc.images[0].data == original
    assert (doc.images[0].width, doc.images[0].height) == (1200, 1600)


def test_rtf_hex_payload_stays_out_of_the_text_layer(tmp_path: Path):
    """Megabytes of hex digits in the text layer would wreck the token
    budget and the text-vs-scan decision."""
    p = fm.build_rtf(tmp_path / "scan.rtf", "Scan below", image=fm.scan_png())
    doc = docsight.extract(p)
    assert len(doc.text) < 500
    assert "pngblip" not in doc.text


def test_rtf_unicode_escapes(tmp_path: Path):
    p = tmp_path / "uni.rtf"
    p.write_bytes(rb"{\rtf1\ansi Jos\u233? Mu\u241?oz}")
    assert "José Muñoz" in docsight.extract(p).text


# --- html / text ----------------------------------------------------------


def test_html_text_tables_and_data_uri_image(tmp_path: Path):
    p = fm.build_html(tmp_path / "cert.html", inline_png=fm.scan_png())
    doc = docsight.extract(p)

    assert doc.kind is DocKind.HTML
    assert "CERTIFICATE OF MARRIAGE" in doc.text
    assert "var a=1" not in doc.text  # script contents
    assert "color:red" not in doc.text  # style contents
    assert doc.tables[0].lookup("Spouse B") == "Samira Okonkwo"
    assert len(doc.images) == 1
    assert doc.images[0].mime == "image/png"


def test_html_reads_sidecar_image_from_disk(tmp_path: Path):
    p = fm.build_html(tmp_path / "cert.html", sidecar=fm.scan_png())
    doc = docsight.extract(p)
    assert [i.name for i in doc.images] == ["sidecar.png"]


def test_html_never_fetches_a_remote_image(tmp_path: Path):
    """Fetching a URL from a submitted document leaks that it was opened
    and invites SSRF."""
    p = fm.build_html(tmp_path / "cert.html")
    doc = docsight.extract(p)
    assert not doc.images
    assert any("remote image not fetched" in w for w in doc.warnings)


def test_html_image_path_cannot_escape_the_folder(tmp_path: Path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(fm.scan_png())
    sub = tmp_path / "sub"
    sub.mkdir()
    page = sub / "page.html"
    page.write_text('<html><body><img src="../secret.png"></body></html>')

    doc = docsight.extract(page)
    assert not doc.images
    assert any("escapes the document folder" in w for w in doc.warnings)


def test_csv_becomes_a_table(tmp_path: Path):
    p = tmp_path / "fields.csv"
    p.write_text("Field,Value\nSpouse A,Dana Alvarez\nSpouse B,Samira Okonkwo\n")
    doc = docsight.extract(p)
    assert doc.kind is DocKind.TEXT
    assert doc.tables[0].lookup("Spouse A") == "Dana Alvarez"


def test_plain_text_document(tmp_path: Path):
    p = tmp_path / "notes.txt"
    p.write_text("CERTIFICATE OF MARRIAGE\nDana Alvarez and Samira Okonkwo\n")
    doc = docsight.extract(p)
    assert doc.kind is DocKind.TEXT
    assert "Dana Alvarez" in doc.text


# --- containers -----------------------------------------------------------


def test_email_attachments_become_children(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "inner.docx",
        fx.inline_image("rId1"),
        media={"scan.png": fm.scan_png()},
        rels={"rId1": "media/scan.png"},
    )
    eml = fm.build_eml(
        tmp_path / "msg.eml",
        {
            "marriage.docx": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                docx.read_bytes(),
            ),
            "photo.png": ("image/png", fm.scan_png()),
        },
    )
    doc = docsight.extract(eml)

    assert doc.kind is DocKind.EMAIL
    assert doc.meta["subject"] == "Dependent verification documents"
    assert len(doc.children) == 2
    assert {c.origin for c in doc.children} == {"marriage.docx", "photo.png"}
    assert {c.kind for c in doc.children} == {DocKind.DOCX, DocKind.IMAGE}
    # The scan inside the attached docx is reachable from the top.
    assert len(doc.all_images) == 2


def test_prepare_all_judges_each_member_separately(tmp_path: Path):
    typed = fx.build_docx(
        tmp_path / "typed.docx",
        fx.para(
            "CERTIFICATE OF MARRIAGE. Dana Alvarez and Samira Okonkwo were "
            "married on 14 June 2019 in Travis County, Texas, under license "
            "TX-2019-884417, recorded by the County Clerk of Travis County."
        ),
    )
    scanned = fx.build_docx(
        tmp_path / "scanned.docx",
        fx.anchored_image("rId1"),
        media={"scan.png": fm.scan_png()},
        rels={"rId1": "media/scan.png"},
    )
    z = fm.build_zip(
        tmp_path / "bundle.zip",
        {"typed.docx": typed.read_bytes(), "scanned.docx": scanned.read_bytes()},
    )

    results = {r.name: r for r in docsight.prepare_all(z, render_fallback=False)}
    assert results["typed.docx"].strategy is Strategy.TEXT_ONLY
    assert results["typed.docx"].needs_vision is False
    assert results["scanned.docx"].strategy is Strategy.IMAGE_ONLY
    assert results["scanned.docx"].needs_vision is True


def test_prepare_all_returns_one_entry_for_a_plain_file(tmp_path: Path):
    p = tmp_path / "photo.png"
    p.write_bytes(fm.scan_png())
    assert len(docsight.prepare_all(p)) == 1


def test_archive_member_failure_does_not_lose_the_others(tmp_path: Path):
    good = fx.build_docx(tmp_path / "good.docx", fx.para("Readable content here."))
    z = fm.build_zip(
        tmp_path / "mixed.zip",
        {
            "good.docx": good.read_bytes(),
            "broken.docx": b"PK\x03\x04" + b"\x00" * 50,
            "mystery.bin": b"\x07\x08\x09\x00\xff\xfe",
        },
    )
    doc = docsight.extract(z)
    assert any(c.origin == "good.docx" for c in doc.children)
    assert any("broken.docx" in w or "mystery.bin" in w for w in doc.warnings)


def test_archive_path_traversal_is_neutralised(tmp_path: Path):
    z = fm.build_zip(
        tmp_path / "evil.zip", {"../../escaped.txt": b"CERTIFICATE text here"}
    )
    doc = docsight.extract(z)
    # The member is read, but only ever as a basename inside scratch space.
    assert not (tmp_path.parent.parent / "escaped.txt").exists()
    assert all(".." not in (c.path.name or "") for c in doc.children)


def test_archive_bomb_is_refused_by_ratio(tmp_path: Path):
    z = fm.build_zip_bomb(tmp_path / "bomb.zip")
    doc = docsight.extract(z)
    assert not doc.children
    assert any("archive bomb" in w for w in doc.warnings)


def test_container_prepare_unions_its_members(tmp_path: Path):
    scanned = fx.build_docx(
        tmp_path / "s.docx",
        fx.anchored_image("rId1"),
        media={"scan.png": fm.scan_png()},
        rels={"rId1": "media/scan.png"},
    )
    z = fm.build_zip(tmp_path / "one.zip", {"s.docx": scanned.read_bytes()})
    ready = docsight.prepare(z)
    assert ready.strategy is Strategy.IMAGE_ONLY
    assert len(ready.images) == 1


# --- unsupported formats --------------------------------------------------


def test_outlook_msg_reports_a_reason_not_just_unknown(tmp_path: Path):
    p = tmp_path / "mail.msg"
    # OLE2 header plus the .msg marker stream name in UTF-16LE.
    p.write_bytes(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        + b"\x00" * 200
        + "__substg1.0".encode("utf-16-le")
        + b"\x00" * 200
    )
    with pytest.raises(docsight.UnsupportedFormat, match="extract-msg"):
        docsight.extract(p)


def test_unreadable_reason_is_available_without_extracting(tmp_path: Path):
    assert "py7zr" in (docsight.unreadable_reason("x.7z") or "")
    assert docsight.unreadable_reason("x.docx") is None


# --- llm layer ------------------------------------------------------------


class FakeClient:
    """Stands in for the OpenAI client; records what it was sent."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": self.reply}}]}


FIELDS = {
    "spouse_a": "first spouse's full name",
    "spouse_b": "second spouse's full name",
    "marriage_date": "date of marriage, ISO 8601",
}


def test_read_sends_images_for_a_scan(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "scan.docx",
        fx.anchored_image("rId1"),
        media={"scan.png": fm.scan_png()},
        rels={"rId1": "media/scan.png"},
    )
    client = FakeClient('{"spouse_a": "Dana Alvarez", "spouse_b": "Samira Okonkwo", "marriage_date": "2019-06-14"}')
    result = docsight.read(docx, FIELDS, client=client, model="qwen-test")

    assert result.ok
    assert result.data["spouse_a"] == "Dana Alvarez"
    assert result.used_vision
    assert result.images_sent == 1
    content = client.calls[0]["messages"][0]["content"]
    assert any(p["type"] == "image_url" for p in content)


def test_read_skips_images_when_text_is_trustworthy(tmp_path: Path):
    docx = fx.build_docx(
        tmp_path / "typed.docx",
        fx.para(
            "CERTIFICATE OF MARRIAGE. Dana Alvarez and Samira Okonkwo were "
            "married on 14 June 2019 in Travis County, Texas, under license "
            "TX-2019-884417, recorded by the County Clerk of Travis County."
        ),
    )
    client = FakeClient('{"spouse_a": "Dana Alvarez"}')
    result = docsight.read(docx, FIELDS, client=client)

    assert result.ok
    assert result.used_vision is False
    assert result.images_sent == 0
    content = client.calls[0]["messages"][0]["content"]
    assert all(p["type"] == "text" for p in content)


def test_read_all_handles_a_bundle(tmp_path: Path):
    typed = fx.build_docx(tmp_path / "t.docx", fx.para("Short note."))
    photo = tmp_path / "p.png"
    photo.write_bytes(fm.scan_png())
    z = fm.build_zip(
        tmp_path / "b.zip",
        {"t.docx": typed.read_bytes(), "p.png": photo.read_bytes()},
    )
    client = FakeClient('{"spouse_a": null}')
    results = docsight.read_all(z, FIELDS, client=client, render_fallback=False)
    assert len(results) == 2
    assert {r.name for r in results} == {"t.docx", "p.png"}


def test_read_reports_an_api_error_instead_of_raising(tmp_path: Path):
    p = tmp_path / "photo.png"
    p.write_bytes(fm.scan_png())

    class Boom(FakeClient):
        def create(self, **kwargs):
            raise RuntimeError("503 service unavailable")

    result = docsight.read(p, FIELDS, client=Boom(""))
    assert not result.ok
    assert "503" in result.error


def test_parse_json_survives_fences_and_prose():
    assert docsight.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert docsight.parse_json('Here you go:\n{"a": 2}\nHope that helps!') == {"a": 2}
    assert docsight.parse_json('{"a": 3}') == {"a": 3}
    assert docsight.parse_json("not json at all") is None
    assert docsight.parse_json("") is None


def test_build_prompt_demands_nulls_over_guesses():
    prompt = docsight.build_prompt(FIELDS)
    assert "spouse_a" in prompt
    assert "null" in prompt
    assert "Never guess" in prompt


def test_short_text_file_is_not_sent_to_vision(tmp_path: Path):
    """A .txt or .csv has no pixels and never will. Routing a short one to
    vision asks for an image that cannot exist, and leaves the caller
    holding an 'unverifiable' verdict on a perfectly readable file."""
    p = tmp_path / "short.csv"
    p.write_text("Field,Value\nSpouse A,Dana Alvarez\n")

    ready = docsight.prepare(p)
    assert ready.strategy is Strategy.TEXT_ONLY
    assert ready.needs_vision is False
    assert not any(n.startswith("WARNING") for n in ready.notes)


def test_short_docx_still_routes_to_vision(tmp_path: Path):
    """The same shortness in a format that *can* hold images must still ask
    for pixels -- that is the scanned-certificate case."""
    p = fx.build_docx(tmp_path / "short.docx", fx.para("See attached."))
    assert docsight.extract(p).strategy is Strategy.IMAGE_ONLY


def test_selfcheck_passes_in_this_environment():
    """The self-check is what a user on a locked-down machine runs to find
    out whether the library works there, so it has to be correct itself."""
    from docsight.selfcheck import run

    assert run(verbose=False) is True
