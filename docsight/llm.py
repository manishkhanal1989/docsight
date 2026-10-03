"""Calling a vision model with an extracted document.

Everything here speaks the OpenAI chat-completions dialect, which is what
vLLM, SGLang, Ollama, LM Studio, OpenRouter, Together, DashScope and
OpenAI itself all serve -- so a Qwen3-VL behind vLLM and a hosted model
differ only by ``base_url``.

The ``openai`` package is optional. ``read()`` takes any client object with
a ``.chat.completions.create`` method, so you can pass your own wrapper, a
test double, or an async-free shim without installing anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .api import Prepared, prepare, prepare_all
from .errors import MissingDependency
from .filters import ImageFilter
from .payload import DEFAULT_MAX_EDGE, Dialect
from .types import Strategy

#: A sensible default for self-hosted Qwen3-VL. Override per call.
DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"


class ChatClient(Protocol):
    """The slice of the OpenAI client this module uses."""

    chat: Any


@dataclass
class Reading:
    """What the model returned for one document, plus how it got there."""

    name: str
    strategy: Strategy
    data: dict[str, Any] | None = None
    raw: str = ""
    used_vision: bool = False
    images_sent: int = 0
    model: str = ""
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None

    def get(self, field_name: str, default: Any = None) -> Any:
        return (self.data or {}).get(field_name, default)


def build_prompt(fields: dict[str, str], *, instructions: str = "") -> str:
    """Compose a field-extraction prompt from a name -> description map.

    Asking for an explicit null beats letting the model guess: a blank is a
    verifiable "not legible", while an invented date is a false positive
    that a human reviewer has no way to spot.
    """
    lines = [
        instructions.strip()
        or "Read this document and extract the fields below.",
        "",
        "Return ONLY a JSON object with exactly these keys:",
    ]
    for name, description in fields.items():
        lines.append(f"  {name}: {description}")
    lines += [
        "",
        "Rules:",
        "- Use null for any field that is absent or not legible. Never guess.",
        "- Copy values exactly as written, including spelling and formatting.",
        "- If the document is not the expected type, set every field to null.",
    ]
    return "\n".join(lines)


def read(
    path: str | Path,
    fields: dict[str, str] | None = None,
    *,
    client: ChatClient | None = None,
    model: str = DEFAULT_MODEL,
    prompt: str | None = None,
    instructions: str = "",
    dialect: Dialect = "openai",
    image_filter: ImageFilter | None = None,
    max_edge: int | None = DEFAULT_MAX_EDGE,
    temperature: float = 0.0,
    max_tokens: int = 1500,
    skip_vision_when_text: bool = True,
    base_url: str | None = None,
    api_key: str | None = None,
    **create_kwargs: Any,
) -> Reading:
    """Extract structured fields from one document, calling the model only
    if the document actually needs it.

    Pass either *fields* (a name -> description map) or a ready *prompt*.

    With *skip_vision_when_text* on, a document whose text layer is
    trustworthy still goes to the model -- but as text, with no images
    attached, which is far cheaper and more accurate than OCR-ing a page
    render of text that was already machine-readable.
    """
    ready = prepare(path, image_filter=image_filter)
    client = client or default_client(base_url=base_url, api_key=api_key)
    return _read_prepared(
        ready,
        fields=fields,
        prompt=prompt,
        instructions=instructions,
        client=client,
        model=model,
        dialect=dialect,
        max_edge=max_edge,
        temperature=temperature,
        max_tokens=max_tokens,
        skip_vision_when_text=skip_vision_when_text,
        create_kwargs=create_kwargs,
    )


def read_all(
    path: str | Path,
    fields: dict[str, str] | None = None,
    *,
    client: ChatClient | None = None,
    model: str = DEFAULT_MODEL,
    prompt: str | None = None,
    instructions: str = "",
    dialect: Dialect = "openai",
    image_filter: ImageFilter | None = None,
    max_edge: int | None = DEFAULT_MAX_EDGE,
    temperature: float = 0.0,
    max_tokens: int = 1500,
    skip_vision_when_text: bool = True,
    base_url: str | None = None,
    api_key: str | None = None,
    **create_kwargs: Any,
) -> list[Reading]:
    """Like :func:`read`, but one reading per document in a container.

    A forwarded email with three attachments yields three readings, each
    judged on its own -- so a typed cover letter costs no vision call while
    the scanned certificate beside it gets one.
    """
    client = client or default_client(base_url=base_url, api_key=api_key)
    return [
        _read_prepared(
            ready,
            fields=fields,
            prompt=prompt,
            instructions=instructions,
            client=client,
            model=model,
            dialect=dialect,
            max_edge=max_edge,
            temperature=temperature,
            max_tokens=max_tokens,
            skip_vision_when_text=skip_vision_when_text,
            create_kwargs=create_kwargs,
        )
        for ready in prepare_all(path, image_filter=image_filter)
    ]


def _read_prepared(
    ready: Prepared,
    *,
    fields: dict[str, str] | None,
    prompt: str | None,
    instructions: str,
    client: ChatClient,
    model: str,
    dialect: Dialect,
    max_edge: int | None,
    temperature: float,
    max_tokens: int,
    skip_vision_when_text: bool,
    create_kwargs: dict[str, Any],
) -> Reading:
    if prompt is None:
        if not fields:
            raise ValueError("pass either fields= or prompt=")
        prompt = build_prompt(fields, instructions=instructions)

    out = Reading(
        name=ready.name,
        strategy=ready.strategy,
        model=model,
        notes=list(ready.notes),
    )

    if ready.strategy is Strategy.EMPTY:
        out.error = "nothing extractable in this document"
        return out

    send_images = ready.needs_vision or not skip_vision_when_text
    if send_images:
        content = ready.to_content(prompt, dialect=dialect, max_edge=max_edge)
        out.images_sent = sum(
            1 for p in content if p.get("type") in ("image_url", "image")
        )
        out.used_vision = out.images_sent > 0
    else:
        content = [
            {"type": "text", "text": prompt},
            {"type": "text", "text": f"Document text:\n\n{ready.text}"},
        ]

    out.notes = list(ready.notes)  # to_content may have appended

    if send_images and out.images_sent == 0 and not ready.text.strip():
        out.error = (
            "document needs vision but no image could be sent, and it has no "
            "text layer to fall back on; treat it as unverifiable"
        )
        return out

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            temperature=temperature,
            max_tokens=max_tokens,
            **create_kwargs,
        )
        out.raw = _message_text(response)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    parsed = parse_json(out.raw)
    if parsed is None:
        out.error = "model response was not parseable JSON"
    else:
        out.data = parsed
    return out


def parse_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences even when told not to, so this
    tries the whole string, then a fenced block, then the outermost braces.
    """
    if not text or not text.strip():
        return None

    for candidate in _json_candidates(text):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return None


def _json_candidates(text: str):
    stripped = text.strip()
    yield stripped

    fence = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.S)
    if fence:
        yield fence.group(1).strip()

    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        yield stripped[start : end + 1]


def _message_text(response: Any) -> str:
    """Read the assistant text out of a chat-completions response.

    Handles both the SDK's objects and a plain dict, so a test double or a
    hand-rolled HTTP client works without adapting.
    """
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
    else:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        content = getattr(getattr(choices[0], "message", None), "content", None)

    if isinstance(content, str):
        return content
    # Some servers return content as a list of parts.
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                out.append(part.get("text") or "")
            else:
                out.append(getattr(part, "text", "") or "")
        return "".join(out)
    return ""


def default_client(
    *, base_url: str | None = None, api_key: str | None = None
) -> ChatClient:
    """Build an OpenAI client, reading the usual environment variables.

    *base_url* points at any OpenAI-compatible server -- for local vLLM,
    ``http://localhost:8000/v1``. A local server needs no real key, so a
    placeholder is supplied when none is set.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise MissingDependency(
            "the openai package",
            "Install it with: pip install openai -- or pass client= with any "
            "object exposing .chat.completions.create",
        ) from exc

    import os

    url = base_url or os.environ.get("DOCSIGHT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    key = api_key or os.environ.get("OPENAI_API_KEY") or "not-needed-for-local"
    return OpenAI(base_url=url, api_key=key) if url else OpenAI(api_key=key)
