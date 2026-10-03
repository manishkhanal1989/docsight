# Contributing to docsight

## Development setup

```bash
git clone https://github.com/manishkhanal1989/docsight
cd docsight
python -m pip install -e ".[dev]"
python -m pytest tests -q
python -m ruff check docsight tests
```

For the LibreOffice-backed paths (legacy `.doc`/`.xls`/`.ppt`, and page
rendering of Office documents), install LibreOffice and either put
`soffice` on `PATH` or set `DOCSIGHT_SOFFICE` to its full path. Tests that
need it skip cleanly when it is absent.

## Adding a format

1. Write a backend in `docsight/backends/` exposing
   `extract(path, **kwargs) -> Document`.
2. Register it in `_load_backends()` in `docsight/api.py`.
3. Teach `docsight/routing.py` to detect it **by content**, not extension.
4. Add a fixture builder in `tests/fixtures_more.py` and tests.

For another OOXML-family format, reuse `docsight/backends/opc.py` — it
already handles relationship resolution, orphan-media sweeping, and core
properties. A new format should be a small walker, not a rewrite.

Fixtures are hand-assembled rather than produced by a writer library,
deliberately: a well-behaved writer will not emit the shapes that break
extractors (anchored images, pictures behind drawing parts, hex-encoded
RTF payloads, orphaned media, tracked deletions, path traversal,
compression bombs). Those shapes are the point of the test suite.

## Release process

Releases publish to PyPI through
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no API
token is stored on any machine or in repository secrets.

### One-time PyPI setup

Do this once, before the first release.

1. Create a PyPI account at <https://pypi.org/account/register/> and
   enable 2FA.
2. Go to <https://pypi.org/manage/account/publishing/> and add a
   **pending publisher** with exactly these values:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `docsight` |
   | Owner | `manishkhanal1989` |
   | Repository name | `docsight` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   The environment name matters: `publish.yml` runs its publish job in a
   GitHub environment called `pypi`, and the OIDC claim will not match if
   this field is left blank.

3. In the GitHub repo, go to **Settings → Environments → New
   environment** and create one named `pypi`. Adding yourself as a
   required reviewer there means every publish waits for your approval,
   which is worth the extra click.

A "pending publisher" is how PyPI lets you claim a name that does not exist
yet — the project is created by the first successful upload.

### Cutting a release

```bash
# 1. Bump the version in pyproject.toml and docsight/__init__.py
# 2. Commit it
git commit -am "Release v0.1.1"
git push

# 3. Tag and create the release; publishing triggers on release publication
git tag v0.1.1
git push origin v0.1.1
gh release create v0.1.1 --generate-notes
```

The workflow refuses to publish if the tag does not match the version in
`pyproject.toml`, so a mistyped tag fails loudly instead of shipping
something other than what the tag claims.

### Publishing by hand instead

If you would rather not use Trusted Publishing, create an API token at
<https://pypi.org/manage/account/token/> and upload directly:

```bash
python -m pip install build twine
python -m build
python -m twine upload dist/*     # username: __token__, password: the token
```

Test it against TestPyPI first if you want a dry run:

```bash
python -m twine upload --repository testpypi dist/*
```

Note that a version number on PyPI can never be reused, even after
deleting the release — so bump the version rather than trying to replace a
bad upload.

## Conventions

- Keep the stdlib-only promise: `import docsight` must not pull in any
  third-party module. The `no-extras` CI job enforces this.
- Extracted image bytes are the original bytes. Any path that re-encodes
  must set `source="rendered"` so callers can tell.
- A document that needs vision but has no sendable image must never look
  like a clean result — say so in `notes` or `warnings`.
- Never fetch a URL that arrived inside a submitted document.
