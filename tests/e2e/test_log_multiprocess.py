"""One rotating log file, several REAL processes writing it at once.

Why this tier exists: magent's log names are not owned by one process. The same
``~/.magent/logs/<name>.log`` is opened by several long-lived magent processes
at the same time -- traced, not guessed:

  * ``hotkey.log``   -- the Alt+V listener process (``python -m magent hotkey``),
    ``magent serve`` (``upload_server._supervise_hotkey`` /
    ``supervision_enabled``), and any foreground ``magent up``/``attach`` that
    runs ``launch.ensure_hotkey_listener``.
  * ``launch.log``   -- foreground ``magent up``/``--go``/``attach``, plus
    ``magent serve`` (every ``psmux.send_keys``/``flash_message`` warning) and
    the listener's F2 path.
  * ``attention.log``-- the ``magent attention -d`` daemon, ``magent watch``,
    and any process that READS the agent-state store and finds an unusable
    record (``agent_state._warn_unusable``).
  * ``upload.log``   -- ``magent serve`` and ``psmux``'s flash timeout warning.
  * ``platform.log`` -- every process that imports a platform backend at all.

``logging.handlers.RotatingFileHandler`` is a single-process design, and two
things break when it is shared:

1. **Rollover renames the live file.** On Windows a second process holding the
   file open makes ``os.rename`` fail with ``WinError 32``; the handler is left
   with no stream, the record is dropped into ``handleError``, and the *next*
   record retries the same doomed rename -- so the file never rotates (unbounded
   growth) and every record from that point is lost while contention lasts. On
   POSIX the rename succeeds but the other process keeps writing into the file
   it renamed away, so those records land in a backup that the next rotation
   overwrites.
2. **Windows has no atomic append.** The CRT implements ``open(path, "a")`` as
   seek-to-end followed by write with nothing holding the file in between, so
   two overlapping writers resolve the same offset and one lands on top of the
   other. (Measured before, in a different guise -- see the comment on
   ``_SHIM_BODY`` in ``test_altv_flash.py``.)

So this tier drives the REAL ``log.get_logger`` machinery from N real Python
processes, released simultaneously off a shared wall-clock start, with a tiny
``maxBytes`` so rotation is forced repeatedly *during* the burst. It asserts on
what ended up on disk: every record present exactly once, no torn line, no
``--- Logging error ---`` on any child's stderr, and -- so the whole thing can
never go vacuous -- that rotation demonstrably happened and the retained files
were nowhere near their capacity (nothing was legitimately aged out).

HOME is fully redirected (``HOME``/``USERPROFILE``/``HOMEDRIVE``/``HOMEPATH``)
because ``log.LOG_DIR`` is ``Path.home() / ".magent" / "logs"``: the children
must never touch the developer's real ``~/.magent``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections import Counter
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

# --- Burst shape -------------------------------------------------------------
# Chosen so the retained files hold every record with headroom to spare, while
# still forcing many rollovers mid-burst. WRITERS * RECORDS records of ~PAD+70
# bytes is ~90 KB against a (BACKUP_COUNT + 1) * MAX_BYTES == 156 KB capacity,
# i.e. roughly 14 rollovers and a ~1.7x margin. The margin is asserted below, so
# a future edit that tunes these numbers into "records were legitimately aged
# out" fails loudly instead of passing vacuously.
WRITERS = 4
RECORDS = 200
PAD = 40
MAX_BYTES = 6_000
BACKUP_COUNT = 25
LOG_NAME = "mplogtest"

# Each child spends most of its startup importing magent; releasing them off a
# shared absolute deadline (rather than "as spawned") is what guarantees the
# writes actually overlap.
STARTUP_GRACE_S = 6.0
CHILD_TIMEOUT_S = 120.0

_CHILD = """
import sys, time
from magent import log

log_name, widx, records, pad, max_bytes, backups, start_at = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
    int(sys.argv[5]), int(sys.argv[6]), float(sys.argv[7]),
)

# White-box on purpose: the tier is about concurrency, not about the production
# constants. Shrinking them is the only way to force many rollovers inside a
# burst short enough to be a test; everything else -- the handler, the file
# naming, the rollover -- is the real product machinery.
log._MAX_BYTES = max_bytes
log._BACKUP_COUNT = backups

logger = log.get_logger(log_name)
payload = "x" * pad

while time.time() < start_at:
    time.sleep(0.005)

for i in range(records):
    logger.info("REC w=%02d i=%05d %s", widx, i, payload)
"""


def _child_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    # log.LOG_DIR is Path.home()/.magent/logs -- redirect every variable
    # Path.home() consults, on every OS, so the real ~/.magent is untouchable.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["HOMEDRIVE"] = os.path.splitdrive(str(home))[0] or ""
    env["HOMEPATH"] = os.path.splitdrive(str(home))[1] or str(home)
    # Nothing here starts serve/attention, but the isolation laws are absolute.
    env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
    env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
    # ...and the psmux priority sweep reaches processes by IMAGE NAME, which
    # no HOME redirect contains: a test-spawned serve/daemon must never
    # re-prioritise the developer's real psmux fleet.
    env["MAGENT_PSMUX_BOOST"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _run_writers(
    tmp_path: Path, count: int
) -> tuple[Path, list[subprocess.CompletedProcess[str]]]:
    """Release ``count`` real writer processes at the same instant. Returns the
    redirected log dir and every child's completed process."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "writer.py"
    script.write_text(_CHILD, encoding="utf-8")

    start_at = time.time() + STARTUP_GRACE_S
    env = _child_env(home)
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                LOG_NAME,
                str(widx),
                str(RECORDS),
                str(PAD),
                str(MAX_BYTES),
                str(BACKUP_COUNT),
                repr(start_at),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for widx in range(count)
    ]
    done: list[subprocess.CompletedProcess[str]] = []
    deadline = time.time() + CHILD_TIMEOUT_S
    try:
        for p in procs:
            remaining = max(1.0, deadline - time.time())
            out, err = p.communicate(timeout=remaining)
            done.append(
                subprocess.CompletedProcess(p.args, p.returncode, out or "", err or "")
            )
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    return home / ".magent" / "logs", done


# Every line the handler writes must be exactly one whole record. The trailing
# anchor is what catches a torn/interleaved write: a line that ends mid-payload,
# or carries a second record's prefix, cannot match.
_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO \d+ "
    rf"magent\.{LOG_NAME} REC w=(\d{{2}}) i=(\d{{5}}) x{{{PAD}}}$"
)


def _harvest(log_dir: Path) -> tuple[list[tuple[int, int]], list[str], list[Path]]:
    """Every parsed record, every unparsable line, and every retained file."""
    files = sorted(log_dir.glob(f"{LOG_NAME}.log*"))
    records: list[tuple[int, int]] = []
    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if not line:
                continue
            m = _LINE.match(line)
            if m:
                records.append((int(m.group(1)), int(m.group(2))))
            else:
                bad.append(f"{path.name}: {line!r}")
    return records, bad, files


class TestConcurrentWriters:
    """N real processes, one log name, rotation forced mid-burst."""

    def test_every_record_survives_rotation_under_contention(self, tmp_path):
        log_dir, procs = _run_writers(tmp_path, WRITERS)

        for i, p in enumerate(procs):
            assert p.returncode == 0, f"writer {i} exited {p.returncode}\n{p.stderr}"

        # The stdlib handler reports a failed rollover by printing
        # "--- Logging error ---" plus a traceback to stderr and DROPPING the
        # record. A shared log must never produce one.
        for i, p in enumerate(procs):
            assert "--- Logging error ---" not in p.stderr, (
                f"writer {i} hit a handler error while logging:\n{p.stderr}"
            )
            assert p.stderr.strip() == "", f"writer {i} wrote to stderr:\n{p.stderr}"

        records, bad, files = _harvest(log_dir)

        assert not bad, (
            f"{len(bad)} torn/interleaved line(s) in the shared log; first 5:\n"
            + "\n".join(bad[:5])
        )

        # Non-vacuity 1: rotation actually happened during the burst.
        assert len(files) >= 4, (
            f"rotation never happened -- only {[f.name for f in files]} exist; "
            "the burst did not exercise rollover at all"
        )

        # Non-vacuity 2: nothing was legitimately aged out, so "missing" below
        # can only mean "lost".
        total = sum(f.stat().st_size for f in files)
        capacity = MAX_BYTES * (BACKUP_COUNT + 1)
        assert total < capacity * 0.75, (
            f"retained bytes {total} are within 75% of the {capacity}-byte "
            "capacity: records may have been aged out by design, which would "
            "make the completeness assertion below meaningless. Raise "
            "BACKUP_COUNT or lower RECORDS."
        )

        expected = {(w, i) for w in range(WRITERS) for i in range(RECORDS)}
        got = Counter(records)
        missing = sorted(expected - set(got))
        dupes = sorted(k for k, n in got.items() if n > 1)
        extra = sorted(set(got) - expected)

        assert not extra, f"records nobody wrote: {extra[:5]}"
        assert not dupes, f"{len(dupes)} duplicated record(s): {dupes[:5]}"
        assert not missing, (
            f"{len(missing)} of {len(expected)} records lost to concurrent "
            f"rotation; first 10: {missing[:10]}\n"
            f"files: {[(f.name, f.stat().st_size) for f in files]}"
        )

    def test_rotation_still_bounded_when_uncontended(self, tmp_path):
        """The fix must not buy safety by never rotating: one writer alone must
        still roll over and still be capped at backupCount + 1 files."""
        log_dir, procs = _run_writers(tmp_path, 1)
        assert procs[0].returncode == 0, procs[0].stderr
        assert procs[0].stderr.strip() == "", procs[0].stderr

        _records, bad, files = _harvest(log_dir)
        assert not bad, f"torn lines with a single writer: {bad[:5]}"
        assert len(files) >= 2, (
            f"a lone writer never rotated: {[f.name for f in files]}"
        )
        assert len(files) <= BACKUP_COUNT + 1, (
            f"rotation unbounded: {len(files)} files {[f.name for f in files]}"
        )
        for f in files:
            # The active file is capped by maxBytes; a rotated one is whatever
            # it was when it crossed the threshold, plus at most one record.
            assert f.stat().st_size <= MAX_BYTES + 4096, (
                f"{f.name} is {f.stat().st_size} bytes, past maxBytes {MAX_BYTES}"
            )
