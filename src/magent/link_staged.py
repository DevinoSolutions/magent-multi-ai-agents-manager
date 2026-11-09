"""Relay uploads handed to ``POST /api/v1/uploads`` by path (spec 4.5).

``link.py`` pulls an upload from the relay and decrypts it into
``~/.magent/uploads/.incoming/<uploadId>/``; the link host then posts
``{staged_path, name, session, inject}`` with Content-Type
``application/x-magent-staged+json`` instead of the bytes. Only a ``linked``
caller may use this form, and only for a regular file inside
``linkfiles.INCOMING_DIR``: anything else would let an API client make the
daemon read an arbitrary local file into an upload. From there the file
takes the multipart form's path: session check, ``uploads.save``, paste,
``upload`` event.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from magent import linkfiles
from magent.wire import WireError

CONTENT_TYPE = "application/x-magent-staged+json"


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    name: str
    session: str
    inject: bool


def is_staged(content_type: str | None) -> bool:
    """Is this request the staged form (not multipart)?"""
    return (content_type or "").split(";")[0].strip().lower() == CONTENT_TYPE


def _text(body: dict[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise WireError("invalid_request", f"{key} is required")
    return value


def load(caller_kind: str, body: dict[str, object]) -> StagedUpload:
    """The staged file a linked caller named, or the WireError refusing it."""
    if caller_kind != "linked":
        raise WireError(
            "forbidden",
            "staged uploads come only from magent link",
            {"reason": "not_linked"},
        )
    staged_path = _text(body, "staged_path")
    name, session = _text(body, "name"), _text(body, "session")
    inject = body.get("inject", True)
    if not isinstance(inject, bool):
        raise WireError("invalid_request", "inject must be true or false")
    incoming = linkfiles.INCOMING_DIR.resolve()
    path = Path(staged_path)
    # Resolved, so neither ".." nor a link inside the folder can point out.
    resolved = path.resolve() if path.is_absolute() else None
    if (
        resolved is None
        or resolved == incoming
        or not resolved.is_relative_to(incoming)
    ):
        raise WireError(
            "forbidden",
            "a staged upload must sit in the link's incoming folder",
            {"reason": "outside_incoming"},
        )
    if not resolved.is_file():
        raise WireError(
            "not_found", "the staged file is gone", {"reason": "staged_missing"}
        )
    return StagedUpload(path=resolved, name=name, session=session, inject=inject)
