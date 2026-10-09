"""The ``/api/v1`` wire envelope, shared by the HTTP surface and every CLI
``--json``/``--v1`` shell so the two can never drift.

``{"ok": true, "data": ...}`` or
``{"ok": false, "error": {"code", "message", "details"?}}``. Stdlib only.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

ErrorCode = Literal[
    "invalid_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "method_not_allowed",
    "conflict",
    "expired",
    "payload_too_large",
    "rate_limited",
    "internal",
    "unavailable",
    "timeout",
]

# HTTP status for each code (spec 3.2).
STATUS: dict[ErrorCode, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "expired": 410,
    "payload_too_large": 413,
    "rate_limited": 429,
    "internal": 500,
    "unavailable": 503,
    "timeout": 504,
}


class WireError(Exception):
    """A refusal that travels as the error envelope. Subsystems raise their
    own subclass (``control.ControlError``, ``projects.ProjectError``); the
    shells map ``code`` to an HTTP status or an exit code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.details = details or {}


def to_wire(value: object) -> object:
    """A dataclass (or a list of them) as plain JSON-ready data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [to_wire(v) for v in value]
    return value


def ok(data: object) -> dict[str, object]:
    return {"ok": True, "data": to_wire(data)}


def error(
    code: ErrorCode, message: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    body: dict[str, object] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"ok": False, "error": body}


def from_exc(exc: WireError) -> dict[str, object]:
    return error(exc.code, exc.message, exc.details)
