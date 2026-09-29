"""The hand-off launcher (``platform/_handoff_launcher.py``), on every OS.

It is the file the scheduled task really runs on the desktop, copied into the
scratch directory as ``launch.py``; the Windows tier in test_desktop_handoff
drives it end to end through a fake Task Scheduler. Here it is driven
directly, against real child processes: the argv file, the spawn, the two
records the poll reads, and the failure that must answer at once.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from magent.platform import _handoff_launcher as launcher

# Every character a quoting layer has ever eaten: the typographic quote
# PowerShell ends literals on, non-ASCII, cmd's two metacharacters, spaces
# and embedded double quotes.
_WEIRD = [
    "O\u2019Brien \u00d1 \u0442",
    "a & b",
    "100% %PATH%",
    'say "hi"',
    " lead and trail ",
    "",
    "back\\slash\\",
]


def _stage(work: Path, argv: list[str], cwd: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    launcher.write_spec(work, argv, str(cwd))
    return work


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheSpecFile:
    def test_it_round_trips_exactly_and_is_ascii(self, tmp_path):
        # Lone surrogates too: Windows allows them in a file name and an
        # argument, and json escapes them, so they survive the file.
        argv = ["C:\\Py \u00d1\\python.exe", *_WEIRD, "\udcff", "\ud800x"]
        cwd = "C:\\w\u2019s \udcff"
        launcher.write_spec(tmp_path, argv, cwd)

        assert (tmp_path / launcher.SPEC).read_bytes().isascii()
        assert launcher.read_spec(tmp_path) == (argv, cwd)

    @pytest.mark.parametrize(
        "body",
        [
            "[]",
            '{"argv": [], "cwd": "x"}',
            '{"argv": ["a", 1], "cwd": "x"}',
            '{"argv": "a", "cwd": "x"}',
            '{"argv": ["a"]}',
            '{"argv": ["a"], "cwd": 3}',
            "not json",
        ],
    )
    def test_anything_else_is_a_value_error(self, tmp_path, body):
        (tmp_path / launcher.SPEC).write_text(body, encoding="ascii")

        with pytest.raises(ValueError):
            launcher.read_spec(tmp_path)


class TestTheRecords:
    def test_a_record_is_one_number_and_a_newline(self, tmp_path):
        launcher.record(tmp_path / "rc.txt", -1073741510)

        assert (tmp_path / "rc.txt").read_bytes() == b"-1073741510\n"
        assert list(tmp_path.iterdir()) == [tmp_path / "rc.txt"]

    def test_a_record_lands_whole_or_not_at_all(self, tmp_path, monkeypatch):
        # The poll reads rc.txt every 250ms and takes the first number it
        # sees; a file written in place can be read empty or half-written. So
        # the final name must appear by rename, already holding the number.
        target = tmp_path / "rc.txt"
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((Path(dst).exists(), Path(src).read_bytes()))
            real_replace(src, dst)

        monkeypatch.setattr(launcher.os, "replace", spy)

        launcher.record(target, 7)

        assert seen == [(False, b"7\n")]
        assert target.read_bytes() == b"7\n"

    @pytest.mark.parametrize(
        ("raw", "signed"),
        [
            (0, 0),
            (7, 7),
            (0x7FFFFFFF, 0x7FFFFFFF),
            (0x80000000, -0x80000000),
            (0xC000013A, -1073741510),
            (0xFFFFFFFF, -1),
            (-9, -9),  # a POSIX signal: already negative, left alone
        ],
    )
    def test_an_exit_code_is_the_signed_int32_windows_reports(self, raw, signed):
        assert launcher.signed_exit_code(raw) == signed


class TestItRunsTheCommand:
    def test_the_child_sees_the_exact_argv_and_cwd(self, tmp_path):
        cwd = tmp_path / "caller \u00d1 \u0442 & 100%"
        cwd.mkdir()
        # The child reports through a json FILE, so no console encoding and no
        # stdout decoding can blur what it received.
        report = tmp_path / "report.json"
        code = (
            "import json, os, sys; "
            f"open({str(report)!r}, 'w', encoding='ascii').write("
            "json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))"
        )
        work = _stage(tmp_path / "work", [sys.executable, "-c", code, *_WEIRD], cwd)

        assert launcher.run(work) == 0

        got = json.loads(report.read_text(encoding="ascii"))
        assert got == {"argv": _WEIRD, "cwd": str(cwd)}
        assert _text(work / launcher.RC) == "0\n"

    def test_its_exit_code_and_streams_are_recorded(self, tmp_path):
        code = (
            "import sys; print('to out'); print('to err', file=sys.stderr); sys.exit(7)"
        )
        work = _stage(tmp_path / "w", [sys.executable, "-c", code], tmp_path)

        assert launcher.run(work) == 7

        assert _text(work / launcher.RC) == "7\n"
        assert _text(work / launcher.OUT).strip() == "to out"
        assert _text(work / launcher.ERR).strip() == "to err"
        assert int(_text(work / launcher.PID)) > 0

    def test_the_pid_lands_before_the_exit_code(self, tmp_path):
        # pid.txt is the poll's "it really started" and rc.txt its "done";
        # a child that waits for pid.txt must never find rc.txt beside it.
        work = tmp_path / "w"
        code = (
            "import pathlib, sys, time; w = pathlib.Path(sys.argv[1]); "
            "deadline = time.monotonic() + 30\n"
            "while not (w / 'pid.txt').exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "print((w / 'pid.txt').exists(), (w / 'rc.txt').exists())"
        )
        _stage(work, [sys.executable, "-c", code, str(work)], tmp_path)

        launcher.run(work)

        assert _text(work / launcher.OUT).split() == ["True", "False"]

    def test_the_child_reads_eof_not_the_launchers_stdin(self, tmp_path):
        # Nobody is at the desktop to type, and an inherited stdin is the
        # task's console: a child that reads it would wait forever. So the
        # launcher runs with a stdin that is open and silent, and the child
        # must still see EOF at once.
        work = tmp_path / "w"
        code = "import sys; print(repr(sys.stdin.read()))"
        _stage(work, [sys.executable, "-c", code], tmp_path)
        script = work / launcher.LAUNCHER
        script.write_bytes(Path(launcher.__file__).read_bytes())

        proc = subprocess.Popen(
            [sys.executable, "-I", str(script)], stdin=subprocess.PIPE, cwd=tmp_path
        )
        try:
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            assert proc.stdin is not None
            proc.stdin.close()

        assert _text(work / launcher.OUT).strip() == "''"
        assert _text(work / launcher.RC) == "0\n"


class TestACommandThatCannotStart:
    def test_it_answers_rc_1_with_the_reason_at_once(self, tmp_path):
        # Without this, "the command is not there" looks like "the task never
        # started" and costs the caller the whole start grace to learn it.
        missing = tmp_path / "no such program.exe"
        work = _stage(tmp_path / "w", [str(missing), "up"], tmp_path)
        started = time.monotonic()

        assert launcher.run(work) == 1

        assert time.monotonic() - started < 30
        assert _text(work / launcher.RC) == "1\n"
        assert not (work / launcher.PID).exists()
        reason = _text(work / launcher.ERR)
        assert reason.startswith("hand-off launcher: could not start the command:")

    def test_a_missing_working_directory_is_the_same_answer(self, tmp_path):
        work = _stage(tmp_path / "w", [sys.executable, "-c", "pass"], tmp_path / "gone")

        assert launcher.run(work) == 1
        assert "could not start the command" in _text(work / launcher.ERR)

    def test_a_malformed_spec_is_the_same_answer(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        (work / launcher.SPEC).write_text("{}", encoding="ascii")

        assert launcher.run(work) == 1
        assert _text(work / launcher.RC) == "1\n"
        assert "is not a hand-off spec" in _text(work / launcher.ERR)


class TestItRunsAsALooseScript:
    def test_python_dash_i_launch_py_records_the_exit_code(self, tmp_path):
        # The production shape exactly: a COPY named launch.py in the scratch
        # directory, run by path under -I, finding its files next to itself.
        work = _stage(
            tmp_path / "w \u00d1",
            [sys.executable, "-c", "raise SystemExit(5)"],
            tmp_path,
        )
        script = work / launcher.LAUNCHER
        script.write_bytes(Path(launcher.__file__).read_bytes())

        subprocess.run(
            [sys.executable, "-I", str(script)],
            check=True,
            timeout=60,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
        )

        assert _text(work / launcher.RC) == "5\n"

    def test_main_runs_the_directory_it_was_copied_into(self, tmp_path, monkeypatch):
        work = _stage(tmp_path / "w", [sys.executable, "-c", "pass"], tmp_path)
        monkeypatch.setattr(launcher, "__file__", str(work / launcher.LAUNCHER))

        launcher.main()

        assert _text(work / launcher.RC) == "0\n"

    def test_it_imports_only_the_standard_library(self):
        # It runs outside the package, from a scratch directory: an import of
        # magent (or of anything not shipped with Python) would fail there
        # even though every in-package test passes.
        source = Path(launcher.__file__).read_bytes()
        assert source.isascii()
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])

        assert names
        assert names <= set(sys.stdlib_module_names) | {"__future__"}

    def test_it_neither_reads_nor_sets_the_environment(self):
        # The child inherits the launcher's environment untouched; everything
        # the hand-off needs set is set by run.ps1, where it is pinned.
        tree = ast.parse(Path(launcher.__file__).read_bytes())
        touched = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and "env" in node.attr.lower())
            or (isinstance(node, ast.Name) and "env" in node.id.lower())
            or (isinstance(node, ast.keyword) and node.arg == "env")
        ]

        assert touched == []
