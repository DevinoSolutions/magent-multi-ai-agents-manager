"""A path this user may not stat, the same way on every Python 3.10-3.14.

Patched at ``os.stat``, never ``Path.stat``: from 3.14 ``Path.is_dir``/
``is_file``/``exists`` do not call ``Path.stat`` (on Windows they skip
``os.stat`` too), and 3.10's ``Path.stat`` bound ``os.stat`` at import. The
product's ``nodes.path_mode`` looks ``os.stat`` up at call time on every
version, so this reaches it everywhere. ``deny_stat`` also gives pathlib the
3.14 contract on every version (``py314_pathlib``): a site that went back to
a ``Path.*`` check reads the denied path as absent everywhere -- 3.13, the
version of the one required check, included -- so its pin fails there too.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def py314_pathlib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.14's existence contract on every version: ``Path.is_dir``/
    ``is_file``/``exists``/``is_symlink`` and ``os.path.isdir``/``isfile``/
    ``exists``/``islink`` answer False for ANY error. Each asks ``os.stat``
    at call time, so it meets ``deny_stat`` on every version too (3.10's Path
    bound ``os.stat`` at import; 3.14's skip it on Windows). ``is_symlink``
    is here because 3.11-3.13 route it through ``os.stat`` and RAISE: a
    ``not path.is_symlink() and path.is_file()`` revert then fails the way
    the vetted check does, and passes a pin 3.14 fails."""

    def answers(
        test: Callable[[int], bool], *, follow: bool = True
    ) -> Callable[..., bool]:
        def check(path: object, *, follow_symlinks: bool = follow) -> bool:
            try:
                mode = os.stat(path, follow_symlinks=follow_symlinks).st_mode
            except (OSError, ValueError):
                return False
            return test(mode)

        return check

    is_dir, is_file = answers(stat.S_ISDIR), answers(stat.S_ISREG)
    exists = answers(lambda _mode: True)
    is_link = answers(stat.S_ISLNK, follow=False)
    for name, check in (
        ("is_dir", is_dir),
        ("is_file", is_file),
        ("exists", exists),
        ("is_symlink", is_link),
    ):
        monkeypatch.setattr(Path, name, check)
    for name, check in (
        ("isdir", is_dir),
        ("isfile", is_file),
        ("exists", exists),
        ("islink", is_link),
    ):
        monkeypatch.setattr(os.path, name, check)


def deny_stat(
    monkeypatch: pytest.MonkeyPatch,
    *denied: Path,
    code: int = errno.EACCES,
    winerror: int | None = None,
) -> None:
    """``os.stat`` of each of ``denied`` raises ``code`` (a deny ACL by
    default); every other path stats as usual. Pathlib answers by the 3.14
    contract meanwhile (``py314_pathlib``)."""
    names = {str(path) for path in denied}
    real_stat = os.stat

    def refusing(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in names:
            error = (code, os.strerror(code), os.fspath(path))
            # A None winerror would read "[WinError None]"; no real one does.
            raise OSError(*error) if winerror is None else OSError(*error, winerror)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refusing)
    py314_pathlib(monkeypatch)


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


def deny_open(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """``os.open`` of each of ``denied`` raises EACCES: a file that stats but
    cannot be opened -- a Windows deny-read ACL, a POSIX mode-000 file. Every
    Python 3.10-3.14 looks ``os.open`` up in the os module at call time, so
    this reaches it; the OS reports that refusal by errno alone (no winerror,
    Windows included), so neither does this."""
    names = {str(path) for path in denied}
    real_open = os.open

    def refusing(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in names:
            code = errno.EACCES
            raise OSError(code, os.strerror(code), os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing)
