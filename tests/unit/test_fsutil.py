"""fsutil: the one home for owner-only and atomic file writes."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

import pytest

from magent import fsutil

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
WRITERS = pytest.mark.parametrize(
    "write",
    [
        pytest.param(fsutil.write_atomic, id="atomic"),
        pytest.param(fsutil.write_private, id="private"),
    ],
)


class TestTheRename:
    """Both writers end in replace_retrying; these pin its contract through
    them, with os.replace as fsutil calls it."""

    @WRITERS
    def test_a_held_target_is_waited_out(self, tmp_path, monkeypatch, write):
        path = tmp_path / "state.json"
        write(path, b"old")
        real_replace = os.replace
        refusals = [PermissionError("held"), PermissionError("held")]
        sleeps: list[float] = []

        def held_twice(src, dst):
            if refusals:
                raise refusals.pop()
            real_replace(src, dst)

        # A scoped context, not monkeypatch.undo(): see the disk-full test.
        with monkeypatch.context() as m:
            m.setattr(os, "replace", held_twice)
            m.setattr(time, "sleep", sleeps.append)
            write(path, b"new")
        assert path.read_bytes() == b"new"
        assert sleeps == [fsutil.REPLACE_SLEEP_S] * 2
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_a_failed_private_replace_keeps_the_old_file(self, tmp_path, monkeypatch):
        path = tmp_path / "secret.json"
        fsutil.write_private(path, b"old")
        attempts: list[str] = []

        def boom(src, dst):
            attempts.append(str(dst))
            raise OSError("disk full")

        with monkeypatch.context() as m:
            m.setattr(os, "replace", boom)
            with pytest.raises(OSError, match="disk full"):
                fsutil.write_private(path, b"new")
        assert attempts == [str(path)]
        assert path.read_bytes() == b"old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["secret.json"]

    @POSIX
    def test_a_temp_name_that_appears_first_is_never_opened(
        self, tmp_path, monkeypatch
    ):
        # O_EXCL: a file planted at the temp name between the pre-clean and
        # the open makes the open fail, never write through the plant.
        path = tmp_path / "secret.json"
        real_open = os.open

        def planted_first(name, flags, *args, **kwargs):
            if str(name).endswith(".tmp"):
                with open(name, "wb") as fh:
                    fh.write(b"plant")
            return real_open(name, flags, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(os, "open", planted_first)
            with pytest.raises(FileExistsError):
                fsutil.write_private(path, b"secret")
        assert list(tmp_path.iterdir()) == []

    @WINDOWS
    def test_a_temp_name_that_appears_first_is_never_created(
        self, tmp_path, monkeypatch
    ):
        # CREATE_NEW: the Windows twin of the O_EXCL test above.
        path = tmp_path / "secret.json"
        real_create = fsutil._win_write_private

        def planted_first(tmp, body):
            with open(tmp, "wb") as fh:
                fh.write(b"plant")
            real_create(tmp, body)

        with monkeypatch.context() as m:
            m.setattr(fsutil, "_win_write_private", planted_first)
            with pytest.raises(FileExistsError):
                fsutil.write_private(path, b"secret")
        assert list(tmp_path.iterdir()) == []


class TestWriteAtomic:
    def test_it_writes_and_creates_the_parent(self, tmp_path):
        path = tmp_path / "a" / "b" / "state.json"
        fsutil.write_atomic(path, b"one")
        assert path.read_bytes() == b"one"

    def test_it_replaces_and_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "state.json"
        fsutil.write_atomic(path, b"one")
        fsutil.write_atomic(path, b"two")
        assert path.read_bytes() == b"two"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_a_failed_replace_keeps_the_old_file(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        fsutil.write_atomic(path, b"old")
        attempts: list[str] = []

        def boom(src, dst):
            attempts.append(str(dst))
            raise OSError("disk full")

        # A scoped context, not monkeypatch.undo(): undo() would also unwind
        # conftest's home tripwire and fail the test.
        with monkeypatch.context() as m:
            m.setattr(os, "replace", boom)
            with pytest.raises(OSError, match="disk full"):
                fsutil.write_atomic(path, b"new")
        assert attempts == [str(path)]
        assert path.read_bytes() == b"old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


class TestWritePrivate:
    def test_it_writes_a_file_check_private_accepts(self, tmp_path):
        path = tmp_path / "secret.json"
        fsutil.write_private(path, b'{"k": 1}')
        assert path.read_bytes() == b'{"k": 1}'
        fsutil.check_private(path)

    def test_a_missing_file_cannot_be_read(self, tmp_path):
        with pytest.raises(fsutil.PrivateFileError, match="cannot be read"):
            fsutil.check_private(tmp_path / "absent")

    def test_a_directory_is_not_a_plain_file(self, tmp_path):
        with pytest.raises(fsutil.PrivateFileError, match="not a plain file"):
            fsutil.check_private(tmp_path)

    @POSIX
    def test_it_is_0600_and_this_users_whatever_the_umask(self, tmp_path):
        old = os.umask(0)
        try:
            fsutil.write_private(tmp_path / "s", b"x")
        finally:
            os.umask(old)
        st = os.lstat(tmp_path / "s")
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert st.st_uid == os.getuid()

    @POSIX
    def test_a_file_others_can_read_is_refused(self, tmp_path):
        path = tmp_path / "s"
        fsutil.write_private(path, b"x")
        path.chmod(0o644)
        with pytest.raises(fsutil.PrivateFileError, match="644"):
            fsutil.check_private(path)

    @POSIX
    def test_a_link_is_refused_and_a_write_replaces_it(self, tmp_path):
        real = tmp_path / "elsewhere"
        real.write_text("untouched\n", encoding="utf-8")
        real.chmod(0o600)
        path = tmp_path / "s"
        path.symlink_to(real)
        with pytest.raises(fsutil.PrivateFileError, match="not a plain file"):
            fsutil.check_private(path)
        fsutil.write_private(path, b"x")
        assert not path.is_symlink()
        assert real.read_text(encoding="utf-8") == "untouched\n"

    @WINDOWS
    def test_its_dacl_names_this_user_alone(self, tmp_path):
        path = tmp_path / "s"
        fsutil.write_private(path, b"x")
        sddl = fsutil._win_dacl(path)
        ace = fsutil._win_canonical_sddl(f"D:P(A;;FA;;;{fsutil._win_user_sid()})")
        assert sddl.startswith("D:P"), sddl
        assert sddl.endswith(ace[len("D:P") :]), sddl
        assert sddl.count("(") == 1, sddl

    @WINDOWS
    def test_an_inherited_acl_is_refused(self, tmp_path):
        path = tmp_path / "s"
        path.write_text("x", encoding="utf-8")
        with pytest.raises(fsutil.PrivateFileError, match="more than this user"):
            fsutil.check_private(path)

    @WINDOWS
    def test_a_widened_acl_is_refused(self, tmp_path):
        path = tmp_path / "s"
        fsutil.write_private(path, b"x")
        done = subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:R"],
            capture_output=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout
        with pytest.raises(fsutil.PrivateFileError):
            fsutil.check_private(path)
