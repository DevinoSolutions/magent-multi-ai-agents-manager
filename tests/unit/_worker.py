"""Run one call on a fresh thread -- where ``magent serve``'s idle reaper and
the attention daemon's readers run -- and hand back its result, or re-raise
what it raised on the caller's thread so the test fails with the real error."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


def on_a_worker_thread(fn: Callable[[], T], *, timeout: float = 60.0) -> T:
    out: dict[str, T] = {}
    err: list[BaseException] = []

    def _run() -> None:
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001  # reason: re-raised on the caller's thread below
            err.append(exc)

    thread = threading.Thread(target=_run, name="test-worker", daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"the worker call did not return within {timeout}s"
    if err:
        raise err[0]
    return out["value"]
