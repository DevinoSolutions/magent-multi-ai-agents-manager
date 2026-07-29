"""Package root. `__version__` is resolved lazily (PEP 562): importing
`importlib.metadata` eagerly costs ~45ms of boot (it drags in email.message,
zipfile and inspect) on every `magent` invocation, including the fast paths
(`--help`, the interactive menu) that never render a version.
"""

from __future__ import annotations

__all__ = ["__version__"]


def __getattr__(name: str) -> str:
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        resolved = version("magent-multi-ai-agents-manager")
    except PackageNotFoundError:  # source tree without an installed dist
        resolved = "0.0.0+unknown"
    # cache so repeat attribute access skips __getattr__ entirely
    globals()["__version__"] = resolved
    return resolved
