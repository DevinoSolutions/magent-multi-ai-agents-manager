"""REAL out-of-repo agent-state writer contract, driven under real node.

WHY THIS TIER EXISTS
--------------------
``magent``'s ``watch`` / ``attention`` / ``status`` never poll agents; they
read per-session state records — ``{state, ts, cwd, session_id}`` — that an
OUT-OF-REPO writer drops into ``~/.magent/state/*.json`` from the agent's
lifecycle hooks. The in-repo ``tests/unit/test_agent_state.py::TestSchemaContract``
pins the shape of what magent *writes and reads*, but nothing here ever ran
the *real external writer*. This tier closes that gap: it installs the actual
published companion package, drives its actual hook under real node the way
Claude Code does, and checks the result against magent's real reader
(``magent.agent_state``, imported — never reimplemented).

WHAT THE REAL WRITER ACTUALLY IS  (read from source, not assumed)
-----------------------------------------------------------------
magent's docs/comments name the writer ``state-sink.mjs``, shipped by the
``ai-agent-notifier`` npm package (DevinoSolutions), wired by
``npx ai-agent-notifier setup`` as Claude Code ``Notification``/``Stop`` hooks.

Driving the real, pinned package (see ``AINS_VERSION``) surfaced a HEADLINE
FINDING, pinned by this tier so it cannot silently change:

  * There is **no ``state-sink.mjs``** in ``ai-agent-notifier`` — not in
    v1.0.6, not in any published version. ``test_no_state_sink_module`` pins it.
  * The package the docs point at is a **pure notifier**. Its real hook entry
    point is ``src/notify.mjs`` (``hooks/hooks.json`` runs
    ``node .../src/notify.mjs --source claude`` on ``Notification`` and
    ``Stop``). It reads the Claude hook event from stdin JSON
    (``{session_id, cwd, hook_event_name}``), then sends a desktop toast / ntfy
    push / terminal bell. It writes its own dedup lock at
    ``~/.ai-agent-notifier/.lock-<source>`` and **nothing else**.
  * It writes **zero** records to ``~/.magent/state/``. So a user who wires
    only ``ai-agent-notifier`` (as README "Where agent states come from"
    instructs) gets an EMPTY magent state store — ``watch`` stays blank.

The real invocation contract this tier reproduces (stdin JSON, per-source
``--source`` arg, per-event mapping) is exactly what ``parse-input.mjs`` /
``hooks.json`` in the installed package define — driven, not guessed.

WHAT THIS TIER ASSERTS (the current, true contract)
---------------------------------------------------
For each realistic Claude hook event (``Stop`` → done/task_complete,
``Notification`` → needs-input, ``SessionStart`` → session_start):

  (1) the real hook actually executed under a fully-redirected HOME — proven by
      its own side effect, the ``~/.ai-agent-notifier/.lock-<source>`` lock; and
  (2) magent's real reader, pointed at ``<HOME>/.magent/state`` (exactly
      where a compliant writer would drop records), sees NOTHING —
      ``agent_state.all_states() == []`` and no record file exists.

TRIPWIRE / UPGRADE PATH
-----------------------
This tier pins a *gap*. The day ``ai-agent-notifier`` (or any wired hook) starts
writing magent state records, assertion (2) flips: ``all_states()`` returns a
record and this test goes RED. That is the signal to promote this tier into a
positive schema-contract test — assert the record lands under the redirected
HOME, parses through ``agent_state`` field-for-field against
``TestSchemaContract``'s ``{state, ts, cwd, session_id}``, and that its state is
in ``agent_state._VALID`` — and to bump ``AINS_VERSION`` to the version that
ships the writer. Do NOT paper over it by adding a fake ``state-sink.mjs``.

ISOLATION
---------
HOME (and on Windows USERPROFILE/HOMEDRIVE/HOMEPATH) is redirected into a
uuid-namespaced tmp dir per event, so the real ``~/.magent`` and
``~/.claude`` are never touched and each event runs against a clean dedup lock
(``notify.mjs`` self-dedups within 10s per HOME+source). No drivers, no servers,
no display — tmp-dir only. Rides the existing ``end-to-end`` job (node is
preinstalled on every GitHub-hosted runner); skips cleanly where node/npm or the
npm registry are unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from typing import TYPE_CHECKING

import pytest

from magent import agent_state

if TYPE_CHECKING:
    from pathlib import Path

# Rides the e2e selection (`-m "e2e and not needs_ssh"`) AND is addressable on
# its own via `-m node_contract`.
pytestmark = [pytest.mark.e2e, pytest.mark.node_contract]

# The EXACT published version this tier pins. Bump consciously: a bump is a
# deliberate re-verification of the real writer's contract (see module docstring
# — a future version that ships a state record writer flips this tier RED).
AINS_VERSION = "1.0.6"
AINS_PKG = "ai-agent-notifier"

# Claude Code hook events -> the meaning ai-agent-notifier's parse-input.mjs maps
# them to (for readability; the mapping lives in the installed package, we only
# feed the raw hook_event_name Claude sends on stdin).
CLAUDE_EVENTS = [
    ("Stop", "task_complete"),
    ("Notification", "needs_input"),
    ("SessionStart", "session_start"),
]


def _child_env(home: Path, **extra: str) -> dict[str, str]:
    """A child env with HOME fully redirected into ``home`` on every OS, and
    every inherited MAGENT_* var stripped. os.homedir() (which notify.mjs
    keys its lock off) reads USERPROFILE on Windows and HOME on POSIX, so both
    are set. Mirrors tests/e2e/test_daemon_lifecycle.py::_child_env."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    env.update(extra)
    return env


@pytest.fixture(scope="session")
def installed_notifier(tmp_path_factory) -> Path:
    """Install the pinned real package ONCE into a tmp prefix and return the path
    to its real hook entry point, ``src/notify.mjs``. Skips (clear message) if
    node/npm or the npm registry are unavailable — this tier must never turn the
    e2e job red on a missing toolchain or a network blip."""
    npm = shutil.which("npm")
    if not shutil.which("node") or not npm:
        pytest.skip("node/npm not on PATH — real-writer contract tier skipped")
    prefix = tmp_path_factory.mktemp("ains_install")
    proc = subprocess.run(
        [
            npm,  # resolved path (npm is npm.cmd on Windows; bare "npm" won't spawn)
            "install",
            "--prefix",
            str(prefix),
            f"{AINS_PKG}@{AINS_VERSION}",
            "--no-audit",
            "--no-fund",
            "--no-package-lock",
            "--loglevel",
            "error",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        # npm on Windows resolves a home for its cache; give it the real env.
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        pytest.skip(
            f"npm install {AINS_PKG}@{AINS_VERSION} failed (network / registry?)"
            f" — real-writer contract tier skipped.\nstderr:\n{proc.stderr}"
        )
    pkg_root = prefix / "node_modules" / AINS_PKG
    notify = pkg_root / "src" / "notify.mjs"
    assert notify.is_file(), (
        f"installed {AINS_PKG}@{AINS_VERSION} but its real hook entry point"
        f" {notify} is missing — the invocation contract changed; re-read"
        f" hooks/hooks.json before updating this tier"
    )
    return notify


def _drive_hook(
    notify: Path,
    home: Path,
    hook_event_name: str,
    session_id: str,
    cwd: str,
    source: str = "claude",
) -> subprocess.CompletedProcess:
    """Run the REAL hook exactly as Claude Code's hooks.json does:
    ``node .../src/notify.mjs --source <source>`` with the hook event delivered
    as JSON on stdin. Returns the completed process (never raises on non-zero —
    the hook is contractually crash-proof and always exits 0)."""
    stdin_payload = json.dumps(
        {"session_id": session_id, "cwd": cwd, "hook_event_name": hook_event_name}
    )
    return subprocess.run(
        [shutil.which("node") or "node", str(notify), "--source", source],
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=60,
        env=_child_env(home),
    )


@pytest.mark.parametrize("hook_event_name,mapped", CLAUDE_EVENTS)
def test_real_writer_populates_nothing_magent_reads(
    installed_notifier, tmp_path, monkeypatch, hook_event_name, mapped
):
    """Drive the real published hook with a realistic Claude ``{hook_event_name}``
    event under a redirected HOME, then check magent's real reader.

    Pins the true current contract (see module docstring): the wired companion
    hook runs, but writes ZERO records into magent's state store — so
    ``agent_state.all_states()`` is empty. Flips RED (forcing a real
    schema-contract test) the day a state-writing hook ships."""
    home = tmp_path / f"home-{uuid.uuid4().hex[:8]}"
    proj = tmp_path / "project"
    proj.mkdir(parents=True)
    home.mkdir()

    # Point magent's REAL reader at exactly where a compliant writer would
    # drop records under this redirected HOME (the reader computes STATE_DIR from
    # Path.home() at import, which is the real home in this test process).
    state_dir = home / ".magent" / "state"
    monkeypatch.setattr(agent_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(agent_state, "_swept_this_process", False)
    monkeypatch.setattr(agent_state, "_warned_files", set())

    result = _drive_hook(
        installed_notifier,
        home,
        hook_event_name,
        session_id=f"sess-{uuid.uuid4().hex}",
        cwd=str(proj),
    )

    # (1) The hook actually executed under OUR HOME — proven by its own side
    # effect (the dedup lock), not by exit code alone (a no-op would also exit 0).
    assert result.returncode == 0, (
        f"real hook exited {result.returncode} for {hook_event_name}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lock = home / ".ai-agent-notifier" / f".lock-{'claude'}"
    assert lock.exists(), (
        f"the real hook left no {lock.name} under the redirected HOME — it did"
        f" not run against our HOME, so this test proves nothing.\n"
        f"stderr:\n{result.stderr}"
    )

    # (2) magent's REAL reader sees NOTHING — the notifier populates no state
    # record for this {hook_event_name} -> {mapped} transition.
    records = agent_state.all_states()
    assert records == [], (
        f"HEADLINE-FINDING TRIPWIRE FLIPPED: the real {AINS_PKG}@{AINS_VERSION}"
        f" hook wrote {len(records)} magent state record(s) for a Claude"
        f" {hook_event_name} event. This tier assumed it writes none. Promote it"
        f" to a positive schema-contract test (see module docstring) and pin the"
        f" schema. Records: {records}"
    )
    assert not any(state_dir.glob("*.json")) if state_dir.exists() else True


def test_no_state_sink_module(installed_notifier):
    """Pin the headline finding at the file level: the package magent's docs
    name as shipping ``state-sink.mjs`` ships no such module (any ``*sink*``
    file). If a future version adds one, this fails — go read it and wire the
    real schema contract."""
    pkg_root = installed_notifier.parent.parent  # .../node_modules/ai-agent-notifier
    sink_like = [
        p.relative_to(pkg_root).as_posix()
        for p in pkg_root.rglob("*")
        if p.is_file() and "sink" in p.name.lower()
    ]
    assert sink_like == [], (
        f"{AINS_PKG}@{AINS_VERSION} now ships sink-like module(s): {sink_like}."
        f" magent's docs say state-sink.mjs is the real writer — verify its"
        f" output against agent_state's schema and update this tier."
    )


def test_pinned_version_is_installed(installed_notifier):
    """Guard that the version actually resolved on disk is the pinned one, so a
    silent registry redirect or cache stale-read can't quietly test a different
    writer than ``AINS_VERSION`` claims."""
    pkg_json = installed_notifier.parent.parent / "package.json"
    data = json.loads(pkg_json.read_text(encoding="utf-8"))
    assert data.get("version") == AINS_VERSION, (
        f"installed {AINS_PKG} is {data.get('version')}, expected pinned {AINS_VERSION}"
    )
