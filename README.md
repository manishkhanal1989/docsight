# docsight

Get text and images out of real-world documents and into a vision model.

Built for document pipelines where the same piece of evidence arrives as a Word file with a scan pasted in, an Excel sheet with a photo floating over the cells, a PowerPoint deck of phone pictures, a forwarded email, or `documents.zip` — and the pipeline has to handle all of them the same way.

```python
import docsight

ready = docsight.prepare("marriage_certificate.docx")

if ready.needs_vision:
    content = ready.to_content("Extract both spouse names and the marriage date.")
    # -> OpenAI-compatible message content, ready for Qwen3-VL and friends
else:
    print(ready.text)   # the text layer was trustworthy; no vision call spent
```

Or let it do the whole call for you:

```python
reading = docsight.read("marriage_certificate.docx", {
    "spouse_a": "first spouse's full name",
    "spouse_b": "second spouse's full name",
    "marriage_date": "date of marriage, ISO 8601",
}, base_url="http://localhost:8000/v1", model="Qwen/Qwen3-VL-8B-Instruct")

print(reading.data)         # {'spouse_a': 'Dana Alvarez', ...}
print(reading.used_vision)  # False if the text layer was enough
```

## Does it work on my machine?

One command, no test files needed � it builds a document in a temp directory
using only the standard library and runs it through the whole pipeline:

```bash
python -m docsight --selfcheck
```

```
docsight 0.1.0
  python        3.12.6 (CPython)
  platform      Windows 11 / AMD64

Optional extras (core formats need none of these):
  no   pymupdf      PDF text and images, page rendering
  no   PIL          TIFF splitting, downscaling, format conversion
  no   openai       docsight.read() convenience client
  no   LibreOffice  legacy .doc/.xls/.ppt, Office page rendering

Pipeline check (synthetic .docx with a floating scan):
  ok    detect format by content  (docx)
  ok    extract text
  ok    find the floating (anchored) image  (1 image(s))
  ok    read image dimensions without Pillow  (1200x1600)
  ok    route to a strategy  (image_only)
  ok    build an OpenAI-compatible payload  (3 parts, 1 image)

RESULT: core pipeline works here.
```

That output is from a bare virtualenv with nothing but `pip` in it. Written
for air-gapped and locked-down machines, where the first question is whether
anything works at all.

## Install

**No installation needed for the core formats.** Copy the `docsight/` folder
next to your script and `import docsight` � it imports no third-party module,
so there is nothing to install, no network access required, and no admin
rights needed. That is the path to use on a machine where `pip` is blocked.

Otherwise:

```bash
pip install docsight              # Word, Excel, PowerPoint, ODF, RTF, HTML, text, images
pip install "docsight[all]"       # adds PDF, HEIC, TIFF splitting, downscaling
pip install "docsight[llm]"       # adds the openai client for docsight.read()

# straight from source, no PyPI needed:
pip install git+https://github.com/manishkhanal1989/docsight
```

Python 3.10 or newer.

The entire OOXML family — `.docx`, `.xlsx`, `.pptx` — plus ODF, RTF, HTML and images needs **no third-party packages at all**. PDF needs PyMuPDF. Legacy `.doc`/`.xls`/`.ppt` and faithful page rendering need LibreOffice:

```bash
apt-get install -y libreoffice-writer      # Docker/Linux
brew install --cask libreoffice            # macOS
# Windows: libreoffice.org/download, or set DOCSIGHT_SOFFICE to the soffice path
```

## Formats

| Input | Text | Images | Needs |
|---|---|---|---|
| `.docx` | yes | original bytes | nothing |
| `.xlsx` / `.xlsm` | per sheet | original bytes | nothing |
| `.pptx` | per slide + notes | original bytes | nothing |
| `.odt` / `.ods` / `.odp` | yes | original bytes | nothing |
| `.rtf` | yes | original bytes | nothing |
| `.html` / `.htm` | yes | data URIs + sidecars | nothing |
| `.txt` / `.csv` / `.tsv` / `.md` | yes | — | nothing |
| PNG/JPEG/GIF/BMP/WebP | — | original bytes | nothing |
| `.pdf` | per page | original bytes | `pymupdf` |
| `.doc` / `.xls` / `.ppt` | via PDF | via PDF | LibreOffice |
| TIFF (multi-page) | — | split per page | `pillow` |
| HEIC | — | yes | `pillow-heif` |
| `.eml` | headers + body | from attachments | nothing |
| `.zip` | from members | from members | nothing |

Routing is by **file content**, not extension — a `.docx` that is really a PDF still parses, a `.doc` renamed to `.xls` routes correctly, and a renamed `.zip` is rejected rather than half-read.

Formats it recognises but can't read (`.msg`, `.7z`, `.pages`) raise `UnsupportedFormat` with the reason and the fix, rather than reporting a corrupt file:

```python
docsight.unreadable_reason("mail.msg")
# 'Outlook .msg is an OLE2 container with no stdlib reader. Convert it to
#  .eml, or install extract-msg and register a backend for it.'
```

## The decision it makes for you

Sending every document to a vision model is the expensive mistake. `prepare()` picks a lane:

| Strategy | When | What to do |
|---|---|---|
| `TEXT_ONLY` | real text layer, no meaningful images | parse the text; skip the vision call |
| `IMAGE_ONLY` | a scan with no usable text | send the image bytes |
| `HYBRID` | both | send pixels, pass the text as a cross-check |
| `EMPTY` | nothing extractable | reject the upload, ask for a resubmit |

Thresholds adapt to the format, because the same character count means different things:

```python
# docsight/strategy.py
FAMILY_THRESHOLDS = {"slides": 400, "sheet": 80, "markup": 120}   # default 180
```

A deck of photographed pages carries titles and "Page 2 of 3" furniture that would clear a prose threshold while holding none of the record — so slides demand more text before they're trusted. A spreadsheet goes the other way: a handful of labelled cells is already a complete, exact answer.

## Why not just use python-docx / openpyxl

You can, and for text they do fine. The difference is what `docsight` refuses to drop.

**Word.** `python-docx` exposes images through `inline_shapes`, which only sees `wp:inline` drawings. A certificate pasted into Word is usually something else:

| How it lands in Word | `inline_shapes` | `docsight` |
|---|---|---|
| Insert → Picture (`wp:inline`) | yes | yes |
| Pasted and dragged, floating (`wp:anchor`) | **no** | yes |
| Legacy template or scanner output (VML) | **no** | yes |
| Dropped in a textbox or shape fill | **no** | yes, flagged `referenced=False` |

**Excel.** Pictures are *never* referenced from worksheet XML. The sheet names a drawing part, and the drawing holds the image relationship. A walker that reads sheets alone sees no images at all — this is the usual reason "my xlsx has a scan in it but nothing came out."

**RTF.** No maintained package extracts both its text and its embedded images. `docsight` parses the control-word stream directly, decodes `\pict` hex payloads, and keeps those megabytes of hex digits out of the text layer.

The anchored and drawing-part cases matter because they fail *quietly*: the pipeline reports "no document found" on a file that plainly contains one.

## Image fidelity

Embedded images come out as the **original bytes** — never re-encoded. Re-rendering a 300 DPI scan through a PDF round-trip measurably costs OCR accuracy on small print like certificate numbers.

```python
doc = docsight.extract("cert.docx")
doc.images[0].data      # byte-identical to word/media/image1.png
doc.images[0].sha256    # stable id for an audit trail
doc.images[0].section   # 'Marriage' (sheet) or 'slide3' — where it came from
```

Only three paths re-encode, and all say so via `source="rendered"`: page rendering, multi-page TIFF splitting, and `normalize()` when it downscales or converts.

## Containers

An email or archive keeps its members distinguishable instead of flattening them:

```python
results = docsight.prepare_all("forwarded.eml")
for r in results:
    print(r.name, r.strategy.value, len(r.images))
# marriage.docx  image_only  1
# cover_note.txt text_only   0
```

Each member is judged on its own, so a typed cover letter costs no vision call while the scanned certificate beside it gets one. `prepare()` on a container treats the whole bundle as one document instead — useful for "find the certificate in here".

Hostile archives are capped on member count, uncompressed size, compression ratio, and nesting depth; member names are flattened to basenames so `../../etc/passwd` can't escape the scratch directory. One unreadable attachment is recorded in `warnings` and never loses the other four.

## Filtering the noise

Documents arrive wrapped in state seals, agency logos, signature scribbles and barcodes. Every one sent to the model costs tokens and returns nothing.

```python
from docsight import ImageFilter

ready = docsight.prepare("cert.docx", image_filter=ImageFilter(
    min_width=300, min_height=300, min_bytes=25_000, max_images=3,
))

for img, reason in ready.rejected:
    print(img.name, reason)   # seal.png 64x64 under minimum
```

Three profiles ship: the default, `STRICT` (large scans only), and `KEEP_ALL` (audit what the file really contained). Kept images sort largest-first, so the page most likely to be the record is the first thing the model sees.

## Calling the model

`docsight.read()` handles prompt, payload, call, and JSON parsing:

```python
FIELDS = {
    "spouse_a": "first spouse's full name",
    "spouse_b": "second spouse's full name",
    "marriage_date": "date of marriage, ISO 8601",
    "certificate_number": "the certificate or license number",
}

reading = docsight.read("cert.docx", FIELDS, base_url="http://localhost:8000/v1")

reading.ok            # False if the call or the parse failed
reading.data          # the parsed dict
reading.used_vision   # whether images were actually sent
reading.images_sent   # how many
reading.notes         # why it chose what it chose
reading.error         # the failure, if any — read() never raises on API errors
```

`read_all()` does the same per member of a container. Pass `client=` any object with `.chat.completions.create` to use your own wrapper or a test double — the `openai` package is optional.

The generated prompt demands `null` over a guess, because a blank is a verifiable "not legible" while an invented date is a false positive a human reviewer has no way to spot.

Prefer to drive the call yourself:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="...")
ready = docsight.prepare("cert.docx")

client.chat.completions.create(
    model="Qwen/Qwen3-VL-8B-Instruct",
    messages=[{"role": "user", "content": ready.to_content("Read this document.")}],
)
```

`to_content()` handles base64 encoding, downscales past a long-edge cap (default 1600px — past that Qwen tiles into more patches for no accuracy gain), converts formats the API would reject, and appends the text layer as a cross-check. Pass `dialect="anthropic"` for Claude.

Images that genuinely cannot be sent are reported, never dropped:

```python
for note in ready.notes:
    print(note)   # 'paste.emf (image/x-emf) is not API-consumable; render the page instead'
```

## Page rendering

When there's no embedded image to extract — a typed certificate, or one pasted as an EMF metafile — rasterise the layout instead:

```python
pages = doc.render_pages(dpi=250)
```

`prepare()` does this automatically as a fallback and records why in `notes` if LibreOffice is absent, rather than raising. 200–300 DPI is the usable band: below ~150 small print degrades, above ~400 the payload grows with no accuracy gain.

## CLI

```bash
python -m docsight cert.docx --text
python -m docsight forwarded.eml                  # expands containers
python -m docsight *.xlsx --filter all --dump ./out
python -m docsight --formats                      # what's supported here
```

```
=== bundle.zip
  container holding 3 document(s)

    kind         xlsx
    strategy     hybrid
    text         164 chars
    tables       2
    images       1 kept, 0 filtered
      + image1.png           image/png        1200x1600     27.5 KB  embedded Marriage
    sheet_names  ['Marriage', 'Notes']
    note         hybrid: 164 body chars (threshold 80), 1 image(s) kept, 0 filtered out
```

## Other things it gets right

- **Tracked changes.** Deleted Word text lives in `w:delText` and is excluded, so a corrected name doesn't read as two conflicting names.
- **Shared strings.** Excel cell text is indirected through a string table; reading sheet XML alone yields indices, not words.
- **Sheet and slide identity.** Worksheet names come from workbook tab order, not filenames, so a prompt saying "the Marriage tab" matches reality. Slide images carry their slide number.
- **Presenter notes.** Captured but marked `notes` — context, never the document itself.
- **Headers and footers.** Captured but kept out of `body_text`, so `CONFIDENTIAL — DO NOT DISTRIBUTE` boilerplate can't inflate the text-vs-scan decision.
- **Document order.** Blocks carry an `order` so a caption stays ahead of the image it introduces. Relative position is evidence.
- **Tables.** `doc.tables[0].lookup("License number")` reads the label/value layout forms and certificates favour, case- and punctuation-insensitively.
- **No outbound requests.** A remote `<img src="https://...">` in submitted HTML is never fetched — that would leak that the file was opened and invite SSRF. It's recorded as a warning. Local image paths that try to escape the document's folder are refused.
- **Nested files.** OLE embeddings and `altChunk` sub-documents are flagged in `warnings` — a document hidden inside an embedded file won't be found by any XML walk, and you should know.
- **Never a quiet pass.** A vision-needing document that ends up with no sendable image gets a loud `WARNING:` note, so "no image to send" can't be mistaken for "nothing suspicious found."

## Extending it

`BACKENDS` maps a `DocKind` to a callable returning a `Document`:

```python
from docsight import BACKENDS, DocKind

BACKENDS[DocKind.PDF] = my_ocr_backed_extractor
```

The OPC plumbing that `.docx`, `.xlsx` and `.pptx` share lives in `docsight/backends/opc.py` — relationship resolution, orphan-media sweeping, core properties — so a new OOXML-family format is a small walker, not a rewrite.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests -q        # 70 tests
```

Fixtures are hand-assembled OOXML, ODF, RTF and archives rather than library output, so the suite can produce the awkward shapes that matter — anchored images, pictures behind drawing parts, hex-encoded RTF payloads, orphaned media, tracked deletions, path traversal, compression bombs — which well-behaved writer libraries won't emit.

## License

MIT
