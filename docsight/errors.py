"""Exception hierarchy."""

from __future__ import annotations


class DocSightError(Exception):
    """Base class for every error raised by docsight."""


class UnsupportedFormat(DocSightError):
    """The file is not a format any registered backend handles."""


class CorruptDocument(DocSightError):
    """The file claims a format but cannot be parsed."""


class MissingDependency(DocSightError):
    """An optional dependency is needed for this operation.

    Carries an actionable install hint rather than a bare ImportError.
    """

    def __init__(self, what: str, hint: str) -> None:
        super().__init__(f"{what} is required for this operation. {hint}")
        self.what = what
        self.hint = hint


class ConversionFailed(DocSightError):
    """An external converter (LibreOffice) ran but produced nothing usable."""
