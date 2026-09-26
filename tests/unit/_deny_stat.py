"""A path this user may not stat, the same way on every Python 3.10-3.14.

Patched at ``os.stat``, never ``Path.stat``: from 3.14 ``Path.is_dir``/
``is_file``/``exists`` do not call ``Path.stat`` (on Windows they skip
``os.stat`` too), and 3.10's ``Path.stat`` bound ``os.stat`` at import. The
product's ``nodes.path_mode`` looks ``os.stat`` up at call time on every
version, so this reaches it everywhere -- while a ``Path.*`` check either
raises (3.11-3.13) or answers from its own probe (3.14): exactly the split a
test of "unknown is never absent" must see.
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def deny_stat(
    monkeypatch: pytest.MonkeyPatch,
    *denied: Path,
    code: int = errno.EACCES,
    winerror: int | None = None,
) -> None:
    """``os.stat`` of each of ``denied`` raises ``code`` (a deny ACL by
    default); every other path stats as usual."""
    names = {str(path) for path in denied}
    real_stat = os.stat

    def stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in names:
            raise OSError(code, os.strerror(code), os.fspath(path), winerror)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def deny_scandir(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """``os.scandir`` of each of ``denied`` raises EACCES: a folder that
    stats but cannot be listed. ``os.walk`` looks ``scandir`` up in the os
    module at call time on every Python 3.10-3.14, so this reaches it."""
    names = {str(path) for path in denied}
    real_scandir = os.scandir

    def scandir(path: object = ".") -> object:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in names:
            code = errno.EACCES
            raise OSError(code, os.strerror(code), os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
