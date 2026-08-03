"""Unit tests for magent.psmux leaf primitives.

Focused on pane_cwd's subprocess guards: the P1-06 extraction (137c8d5) that
moved the inline session_picker ``cwd()`` closure into psmux.pane_cwd dropped
its timeout=3 / encoding=utf-8 / errors=replace / OSError-swallow guards. These
pins restore and lock them so a psmux that hangs, emits non-utf-8 bytes, or
isn't launchable can never take down a caller (the attention picker fans
pane_cwd across every live session concurrently).
"""

from __future__ import annotations

import re
import subprocess
import unicodedata

import pytest

from magent import psmux
from magent.config import MagentConfig, ProjectConfig, Settings


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess with just the fields
    pane_cwd reads."""

    def __init__(self, returncode: int = 0, stdout: str | None = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class TestCapturePane:
    def test_returns_pane_text_with_guards(self, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return _FakeCompleted(returncode=0, stdout="PS C:\\proj> claude\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert psmux.capture_pane("sess", psmux="psmux") == "PS C:\\proj> claude\n"
        assert captured["cmd"][:4] == ["psmux", "-L", "sess", "capture-pane"]
        assert captured["timeout"] == 3
        assert captured["errors"] == "replace"

    def test_failure_degrades_to_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: (_ for _ in ()).throw(OSError("no psmux")),
        )
        assert psmux.capture_pane("sess", psmux="psmux") == ""


class TestPaneCwd:
    def test_targets_the_named_session_explicitly(self, monkeypatch):
        # Regression pin: without `-t <name>`, display-message answers for the
        # CALLING client's own pane. Verified live -- running the picker from
        # inside a psmux session reported the caller's cwd for 41 of 42
        # sessions, so every status row keyed off the wrong project.
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeCompleted(returncode=0, stdout="/home/proj\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert psmux.pane_cwd("sess", psmux="psmux") == "/home/proj"
        cmd = captured["cmd"]
        assert cmd[:4] == ["psmux", "-L", "sess", "display-message"]
        assert cmd[cmd.index("-t") + 1] == "sess"
        assert "#{pane_current_path}" in cmd

    def test_passes_timeout_encoding_and_errors_guards(self, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return _FakeCompleted(returncode=0, stdout="/home/proj\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        result = psmux.pane_cwd("sess", psmux="psmux")

        assert result == "/home/proj"
        assert captured["timeout"] == 3
        assert captured["encoding"] == "utf-8"
        assert captured["errors"] == "replace"
        assert captured["check"] is False
        assert captured["capture_output"] is True

    def test_nonzero_returncode_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeCompleted(returncode=1, stdout="ignored"),
        )
        assert psmux.pane_cwd("sess", psmux="psmux") == ""

    def test_none_stdout_is_guarded(self, monkeypatch):
        # `(result.stdout or "")` must survive a None stdout, not raise.
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeCompleted(returncode=0, stdout=None),
        )
        assert psmux.pane_cwd("sess", psmux="psmux") == ""

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("not launchable"),
            subprocess.TimeoutExpired(cmd="psmux", timeout=3),
        ],
    )
    def test_subprocess_failure_returns_empty(self, monkeypatch, exc):
        def _raise(cmd, **kwargs):
            raise exc

        monkeypatch.setattr(subprocess, "run", _raise)
        assert psmux.pane_cwd("sess", psmux="psmux") == ""

    def test_no_binary_returns_empty(self, monkeypatch):
        # No psmux passed and none on PATH -> "" without touching subprocess.
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.pane_cwd("sess") == ""


class TestPaneCurrentCommand:
    def test_targets_the_named_session_explicitly(self, monkeypatch):
        # Regression pin: without `-t <name>`, display-message answers for the
        # CALLING client's own pane -- and magent commands are routinely run
        # from inside a psmux session, so revive would read the wrong pane.
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return _FakeCompleted(returncode=0, stdout="pwsh\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert psmux.pane_current_command("sess", psmux="psmux") == "pwsh"
        cmd = captured["cmd"]
        assert cmd[:4] == ["psmux", "-L", "sess", "display-message"]
        assert cmd[cmd.index("-t") + 1] == "sess"
        assert "#{pane_current_command}" in cmd
        assert captured["timeout"] == 3
        assert captured["encoding"] == "utf-8"
        assert captured["errors"] == "replace"
        assert captured["check"] is False

    def test_nonzero_returncode_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeCompleted(returncode=1, stdout="pwsh"),
        )
        assert psmux.pane_current_command("sess", psmux="psmux") == ""

    def test_subprocess_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: (_ for _ in ()).throw(OSError("no psmux")),
        )
        assert psmux.pane_current_command("sess", psmux="psmux") == ""

    def test_no_binary_returns_empty(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.pane_current_command("sess") == ""


class TestAgentIdle:
    @pytest.mark.parametrize(
        ("foreground", "idle"),
        [
            ("pwsh", True),
            ("pwsh.exe", True),
            ("PWSH.EXE", True),
            ("powershell", True),
            ("/usr/bin/bash", True),
            ("C:\\Program Files\\PowerShell\\7\\pwsh.exe", True),
            ("  pwsh  ", True),
            ("claude", False),
            ("PING", False),
            ("node", False),
            # Unreadable pane: never inject into a session we can't read.
            ("", False),
            # Deliberate exclusion (see _IDLE_SHELLS): on Windows the agent
            # launchers are .cmd shims, so cmd.exe is the foreground command
            # while an agent boots -- calling it idle would type a second
            # command into a live agent.
            ("cmd", False),
            ("cmd.exe", False),
        ],
    )
    def test_classification(self, monkeypatch, foreground, idle):
        monkeypatch.setattr(
            psmux, "pane_current_command", lambda name, psmux=None: foreground
        )
        assert psmux.agent_idle("sess", psmux="psmux") is idle


class _FakePopen:
    """Stand-in for one fanned-out `display-message` probe, logging when it is
    read so the spawn-then-read ordering can be pinned."""

    def __init__(self, name, stdout, returncode, events, timeout=False):
        self._name = name
        self._stdout = stdout
        self.returncode = returncode
        self._events = events
        self._timeout = timeout
        self.killed = False

    def communicate(self, timeout=None):
        self._events.append(f"read:{self._name}")
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="psmux", timeout=timeout or 0)
        return self._stdout, ""

    def kill(self):
        self.killed = True


class TestPaneCurrentCommands:
    def _fan(self, monkeypatch, results, timeouts=()):
        events: list[str] = []

        def _fake_popen(cmd, **kwargs):
            name = cmd[2]
            events.append(f"spawn:{name}")
            return _FakePopen(
                name,
                results.get(name, ("", 0))[0],
                results.get(name, ("", 0))[1],
                events,
                timeout=name in timeouts,
            )

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        return events

    def test_returns_one_reading_per_session(self, monkeypatch):
        self._fan(monkeypatch, {"a": ("claude\n", 0), "b": ("pwsh\n", 0)})
        assert psmux.pane_current_commands(["a", "b"], psmux="psmux") == {
            "a": "claude",
            "b": "pwsh",
        }

    def test_every_probe_is_spawned_before_any_is_read(self, monkeypatch):
        # The point of the fan-out: n concurrent psmux round-trips, not n
        # sequential ones -- `status` must stay fast at 40+ sessions.
        events = self._fan(
            monkeypatch, {"a": ("claude", 0), "b": ("claude", 0), "c": ("claude", 0)}
        )
        psmux.pane_current_commands(["a", "b", "c"], psmux="psmux")
        assert events == [
            "spawn:a",
            "spawn:b",
            "spawn:c",
            "read:a",
            "read:b",
            "read:c",
        ]

    def test_targets_each_session_explicitly(self, monkeypatch):
        argvs: list[list[str]] = []

        def _fake_popen(cmd, **kwargs):
            argvs.append(cmd)
            return _FakePopen(cmd[2], "claude", 0, [])

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        psmux.pane_current_commands(["sess"], psmux="psmux")
        cmd = argvs[0]
        assert cmd[:4] == ["psmux", "-L", "sess", "display-message"]
        assert cmd[cmd.index("-t") + 1] == "sess"
        assert "#{pane_current_command}" in cmd

    def test_nonzero_and_timeout_degrade_to_empty(self, monkeypatch):
        self._fan(monkeypatch, {"a": ("claude", 1), "b": ("x", 0)}, timeouts=("b",))
        assert psmux.pane_current_commands(["a", "b"], psmux="psmux") == {
            "a": "",
            "b": "",
        }

    def test_unlaunchable_probe_degrades_to_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda cmd, **kw: (_ for _ in ()).throw(OSError("no psmux")),
        )
        assert psmux.pane_current_commands(["a"], psmux="psmux") == {"a": ""}

    def test_no_binary_returns_empty_readings(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.pane_current_commands(["a", "b"]) == {"a": "", "b": ""}

    def test_no_names_spawns_nothing(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda cmd, **kw: pytest.fail("spawned a probe for zero sessions"),
        )
        assert psmux.pane_current_commands([], psmux="psmux") == {}


class TestIsIdleCommand:
    """`agent_idle` now delegates here, so a caller that already holds the
    reading (status's session table) classifies it without a second probe."""

    @pytest.mark.parametrize(
        ("reading", "idle"),
        [("pwsh", True), ("C:\\x\\bash.exe", True), ("claude", False), ("", False)],
    )
    def test_classification_matches_agent_idle(self, reading, idle):
        assert psmux.is_idle_command(reading) is idle


def _cfg(projects, **settings):
    return MagentConfig(projects=projects, base_dir=None, settings=Settings(**settings))


class TestEligibleProjectsDedupe:
    def test_duplicate_project_yields_one_entry(self):
        # Two config entries for the same project used to produce two identical
        # status rows -> two identically-titled attach windows, the second of
        # which could never be tiled. First occurrence wins.
        cfg = _cfg(
            [
                ProjectConfig(path="/a/api", tool="claude"),
                ProjectConfig(path="/b/api", tool="codex"),
            ]
        )
        out = psmux.eligible_projects(cfg)
        assert [p["session"] for p in out] == ["api"]
        assert out[0]["tool"] == "claude"
        assert out[0]["path"] == "/a/api"


class TestPsmuxStatusDownReason:
    """A project that is short-circuited to down -- never probed at all --
    carries WHY. The live case that motivated this: a project whose folder was
    deleted reported down forever and `up` silently skipped it, with no output
    anywhere naming the folder as the problem."""

    def _status(self, monkeypatch, cfg, *, binary="psmux"):
        monkeypatch.setattr(psmux, "find_psmux", lambda: binary)
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *a, **k: pytest.fail("probed a project that cannot be probed"),
        )
        _up, down, _all = psmux.psmux_status(cfg)
        return down

    def test_unresolved_folder_says_so(self, monkeypatch):
        cfg = _cfg([ProjectConfig(path="/nope/eBay", tool="claude")])
        down = self._status(monkeypatch, cfg)
        assert [d["name"] for d in down] == ["eBay"]
        assert down[0]["reason"] == "folder not found"

    def test_empty_command_says_so(self, monkeypatch, tmp_path):
        proj = tmp_path / "api"
        proj.mkdir()
        cfg = _cfg([ProjectConfig(path=str(proj), tool="claude")], tools={})
        down = self._status(monkeypatch, cfg)
        assert down[0]["reason"] == "no agent command"

    def test_missing_binary_wins_over_the_per_project_reason(
        self, monkeypatch, tmp_path
    ):
        # Precedence is binary-first: with no psmux installed nothing can be
        # brought up anyway, so naming the machine-wide blocker beats naming a
        # folder the user still could not launch.
        cfg = _cfg([ProjectConfig(path="/nope/eBay", tool="claude")])
        down = self._status(monkeypatch, cfg, binary=None)
        assert down[0]["reason"] == "psmux not installed"

    def test_a_probed_session_that_is_simply_down_carries_no_reason(
        self, monkeypatch, tmp_path
    ):
        # Ordinary down (has-session said no) is self-explanatory -- annotating
        # it would put "(...)" next to every session on a cold machine.
        proj = tmp_path / "api"
        proj.mkdir()
        cfg = _cfg([ProjectConfig(path=str(proj), tool="claude")])

        class _NoSession:
            def wait(self):
                return 1

        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _NoSession())
        _up, down, _all = psmux.psmux_status(cfg)
        assert [d["name"] for d in down] == ["api"]
        assert "reason" not in down[0]


class TestReviveSessions:
    """A psmux session whose agent was Ctrl-C'ed still answers `has-session`,
    so up/attach reuse it and hand back a window at a bare prompt. Revive
    re-sends the agent command to exactly those panes."""

    def _run(self, monkeypatch, *, idle, only=None, sent_ok=True):
        sent: list[tuple] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "has_session", lambda name, psmux=None: True)
        monkeypatch.setattr(psmux, "agent_idle", lambda name, psmux=None: name in idle)

        def _fake_send(name, *keys, target=None, psmux=None):
            sent.append((name, keys, target))
            return sent_ok

        monkeypatch.setattr(psmux, "send_keys", _fake_send)
        cfg = _cfg(
            [
                ProjectConfig(path="/a/api", tool="claude"),
                ProjectConfig(path="/a/web", tool="claude"),
            ]
        )
        return psmux.revive_sessions(cfg, only=only), sent

    def test_only_the_idle_pane_is_revived(self, monkeypatch):
        revived, sent = self._run(monkeypatch, idle={"api"})
        assert revived == ["api"]
        assert [s[0] for s in sent] == ["api"]

    def test_sends_the_agents_resume_command_and_enter(self, monkeypatch):
        # claude's registry default is `claude --continue`, which picks the
        # dead pane's conversation back up rather than starting a fresh chat.
        _, sent = self._run(monkeypatch, idle={"api"})
        name, keys, target = sent[0]
        assert "claude --continue" in keys[0]
        assert keys[-1] == "Enter"
        # -t is required: send-keys without it can land in the caller's pane.
        assert target == name

    def test_busy_pane_is_left_alone(self, monkeypatch):
        revived, sent = self._run(monkeypatch, idle=set())
        assert revived == []
        assert sent == []

    def test_only_filter_restricts_candidates(self, monkeypatch):
        revived, sent = self._run(monkeypatch, idle={"api", "web"}, only=["web"])
        assert revived == ["web"]
        assert [s[0] for s in sent] == ["web"]

    def test_failed_send_is_not_reported_as_revived(self, monkeypatch):
        revived, _ = self._run(monkeypatch, idle={"api"}, sent_ok=False)
        assert revived == []

    def test_no_psmux_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        cfg = _cfg([ProjectConfig(path="/a/api", tool="claude")])
        assert psmux.revive_sessions(cfg) == []


# The status-right hint, restated here on purpose: an independent copy is what
# makes these pins catch a drive-by restyle instead of following it.
_EXPECTED_HINT = (
    "#[bold,fg=cyan] F1 #[default]Proj. Picker   "
    "#[bold,fg=cyan] F2 #[default]</> VS Code "
)

# ...and what a machine with no VS Code gets instead: the F1 half alone. F2 is
# the hotkey listener's, and the listener needs `code` on PATH -- advertising
# it on a box without VS Code is a lie the user can only discover by pressing
# the key. Restated here for the same reason as the full hint: a restyle in
# psmux.py must be a deliberate edit here too.
_EXPECTED_HINT_F1_ONLY = "#[bold,fg=cyan] F1 #[default]Proj. Picker "


def _visible_cells(status: str) -> int:
    """Worst-case columns `status` occupies. tmux style directives are free.

    Wide and fullwidth characters cost 2 cells; so does an *ambiguous*-width
    one, which a terminal may render either way -- the budget is only safe if
    it assumes the wide rendering. Today's hint is pure ASCII (pinned below),
    so the 2-cell term contributes nothing; it stays so the budget check keeps
    telling the truth if a wide glyph ever sneaks back in.
    """
    text = re.sub(r"#\[[^\]]*\]", "", status)
    return sum(
        2 if unicodedata.east_asian_width(ch) in {"W", "F", "A"} else 1 for ch in text
    )


class TestDecorateSession:
    """The status-line hints (badged `F1`/`F2` keys with spelled-out labels),
    the product-owned `bind -n F1 detach-client`, and the product-owned
    status-left brand -- each half paired with the length budget it needs. All
    are `-L <name>`-scoped so they land on that session's own server and beat
    whatever its tmux.conf set at start-up.
    """

    def _run(
        self, monkeypatch, *, boom: Exception | None = None, code_hint: bool = True
    ):
        cmds: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            cmds.append(cmd)
            if boom is not None:
                raise boom
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        # Pinned, never probed: the dev box and the CI runners disagree about
        # whether `code` is on PATH, and a hint pin that reads the ambient
        # PATH pins nothing.
        monkeypatch.setattr(psmux, "code_on_path", lambda: code_hint)
        psmux.decorate_session("api", psmux="psmux")
        return cmds

    def test_binds_f1_to_detach_client(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[0] == ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"]

    def test_sets_the_status_right_hint(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[1] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right",
            _EXPECTED_HINT,
        ]

    def test_sets_the_status_right_length_alongside_the_hint(self, monkeypatch):
        # Same load-bearing pairing as the brand below: the badged hint is far
        # wider than the old text, and a personal tmux.conf with a tighter
        # status-right-length would cut it mid-label.
        cmds = self._run(monkeypatch)
        assert cmds[2] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right-length",
            "40",
        ]

    def test_sets_the_status_left_brand(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[3] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left",
            "#[bold,fg=green] magent #[default]",
        ]

    def test_sets_the_status_left_length_alongside_the_brand(self, monkeypatch):
        # Load-bearing: a personal tmux.conf with a tighter status-left-length
        # would truncate the brand mid-word, so magent sets both or neither.
        cmds = self._run(monkeypatch)
        assert cmds[4] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left-length",
            "10",
        ]

    @pytest.mark.parametrize("code_hint", [True, False])
    def test_the_argv_is_the_five_decorations(self, code_hint):
        # The list is what every call site fans out, so its shape is contract:
        # a sixth command (or a dropped one) has to be a deliberate edit here.
        # Gating F2 changes the hint TEXT, never the command shape -- dropping
        # a `set` would leave a personal tmux.conf's value in place.
        argv = psmux.decoration_argv("api", "psmux", code_hint)
        assert len(argv) == 5
        assert argv[0][3:] == ["bind", "-n", "F1", "detach-client"]
        assert [cmd[5] for cmd in argv[1:]] == [
            "status-right",
            "status-right-length",
            "status-left",
            "status-left-length",
        ]

    def test_status_left_length_fits_the_brand(self):
        # The number is only correct relative to the brand text; pin the
        # relationship, not just the two literals.
        assert int(psmux._STATUS_BRAND_LEN) >= len(" magent ")

    @pytest.mark.parametrize("code_hint", [True, False])
    def test_status_right_length_fits_the_hint(self, code_hint):
        # Same relationship on the other half. Style directives are free; count
        # cells, not characters, so a future wide glyph cannot shrink the check.
        # Both variants: the F1-only budget is a smaller number guarding a
        # shorter string, and it has to keep fitting it.
        hint, budget = psmux.status_hints(code_hint)
        assert int(budget) >= _visible_cells(hint)

    @pytest.mark.parametrize("code_hint", [True, False])
    def test_hint_is_pure_ascii(self, code_hint):
        # The load-bearing invariant behind the 3.10.3 hotfix: an East-Asian
        # AMBIGUOUS-width glyph (the U+2630 menu hamburger) made psmux and
        # Windows Terminal disagree on cell arithmetic -- a stray highlighted
        # cell inside the bar and a wrapped phantom row under it. A status bar
        # needs the renderer and the multiplexer to agree on width, so the hint
        # stays ASCII-only. This must fail before any glyph goes back in --
        # for every variant, and for each half on its own, so a glyph cannot
        # sneak into the half the default probe happens not to emit.
        assert psmux.status_hints(code_hint)[0].isascii()
        assert psmux._STATUS_HINTS_F1.isascii()
        assert psmux._STATUS_HINTS_F2.isascii()
        assert psmux._STATUS_HINTS.isascii()

    def test_hint_advertises_both_keys(self):
        # The literal is defined once; a rename must not silently drop a key.
        argv = psmux.decoration_argv("api", "psmux", True)
        assert "F1" in argv[1][-1] and "F2" in argv[1][-1]

    def test_hint_badges_each_key_name(self):
        # The readability fix: each key name is its own bold accent badge, so
        # "F1"/"F2" can't read as words in the label next to them.
        hint = psmux.decoration_argv("api", "psmux", True)[1][-1]
        assert "#[bold,fg=cyan] F1 #[default]" in hint
        assert "#[bold,fg=cyan] F2 #[default]" in hint

    def test_hint_spells_out_what_each_key_does(self):
        # The other half of the fix: " F1 picker  F2 code " told the user
        # nothing. The labels, not the styling, are what must survive.
        hint = psmux.decoration_argv("api", "psmux", True)[1][-1]
        assert "Proj. Picker" in hint
        assert "VS Code" in hint
        assert "</> VS Code" in hint

    def test_brand_names_the_product(self):
        # Same guard on the other literal: the status-left is branding, so the
        # product name is the part that must survive a restyle.
        argv = psmux.decoration_argv("api", "psmux", True)
        assert "magent" in argv[3][-1]

    def test_the_full_hint_is_the_two_halves_with_a_three_column_seam(self):
        # The split must not change what a VS-Code machine renders: the seam
        # is still exactly three columns wide, as it was when the hint was one
        # literal. Off-by-one here is invisible in review and obvious on screen.
        assert psmux._STATUS_HINTS == _EXPECTED_HINT
        assert psmux._STATUS_HINTS == (
            _EXPECTED_HINT_F1_ONLY + psmux._STATUS_HINTS_GAP + psmux._STATUS_HINTS_F2
        )
        # One trailing space on the F1 half + the two-space seam = 3 columns.
        assert psmux._STATUS_HINTS_F1.endswith(" ")
        assert not psmux._STATUS_HINTS_F1.endswith("  ")
        assert psmux._STATUS_HINTS_GAP == "  "

    def test_with_code_the_hint_is_the_full_text_and_budget(self, monkeypatch):
        cmds = self._run(monkeypatch, code_hint=True)
        assert cmds[1][-1] == _EXPECTED_HINT
        assert cmds[2][-1] == "40"

    def test_without_code_the_f2_half_is_gone_from_every_argv(self, monkeypatch):
        # The user-visible promise: on a machine with no VS Code, nothing in
        # what magent sends mentions the key it cannot honour.
        cmds = self._run(monkeypatch, code_hint=False)
        assert cmds[1][-1] == _EXPECTED_HINT_F1_ONLY
        assert cmds[2][-1] == psmux._STATUS_HINTS_F1_LEN
        flat = " ".join(arg for cmd in cmds for arg in cmd)
        assert "</>" not in flat
        assert "VS Code" not in flat
        assert "F2" not in flat

    def test_without_code_the_budget_is_smaller_than_the_full_one(self):
        # Not just "some number": dropping half the text without dropping the
        # budget would leave the constant documenting a string it no longer
        # guards.
        assert int(psmux._STATUS_HINTS_F1_LEN) < int(psmux._STATUS_HINTS_LEN)
        assert int(psmux._STATUS_HINTS_F1_LEN) >= _visible_cells(psmux._STATUS_HINTS_F1)

    def test_f1_is_bound_either_way(self, monkeypatch):
        # F1 is magent's own psmux binding, installed right here -- it is true
        # for any viewer of the session, so no probe may gate it.
        for hint in (True, False):
            cmds = self._run(monkeypatch, code_hint=hint)
            assert cmds[0] == [
                "psmux",
                "-L",
                "api",
                "bind",
                "-n",
                "F1",
                "detach-client",
            ]

    def test_code_hint_defaults_to_probing_this_machine(self, monkeypatch):
        # `code_hint=None` means "ask here" -- the decorating machine is the
        # one whose PATH decides.
        cmds: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **k: (
                cmds.append(cmd),
                _FakeCompleted(returncode=0, stdout=""),
            )[1],
        )
        monkeypatch.setattr(psmux, "code_on_path", lambda: False)
        psmux.decorate_session("api", psmux="psmux")
        assert cmds[1][-1] == _EXPECTED_HINT_F1_ONLY

    def test_code_on_path_asks_shutil_for_code(self, monkeypatch):
        # The one owner of the probe; hotkey.py::_do_open_code resolves the
        # very same name, and the hint must not promise a different binary.
        asked: list[str] = []
        monkeypatch.setattr(
            psmux.shutil, "which", lambda n: asked.append(n) or "/usr/bin/code"
        )
        assert psmux.code_on_path() is True
        assert asked == ["code"]
        monkeypatch.setattr(psmux.shutil, "which", lambda n: None)
        assert psmux.code_on_path() is False

    def test_never_raises_when_the_subprocess_fails(self, monkeypatch):
        # A status bar is cosmetic: an unlaunchable/hung psmux is logged and
        # swallowed, never propagated into a bring-up.
        cmds = self._run(monkeypatch, boom=OSError("no psmux"))
        assert len(cmds) == 5  # all attempted; none escaped

    def test_never_raises_on_timeout(self, monkeypatch):
        self._run(monkeypatch, boom=subprocess.TimeoutExpired("psmux", 3))

    def test_no_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)

        def _boom(*a, **k):
            raise AssertionError("must not shell out without a psmux binary")

        monkeypatch.setattr(subprocess, "run", _boom)
        psmux.decorate_session("api")

    def test_fan_out_covers_every_name(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "code_on_path", lambda: True)
        monkeypatch.setattr(
            psmux,
            "decorate_session",
            lambda n, psmux=None, code_hint=None: seen.append(n),
        )
        assert psmux.decorate_sessions(["api", "web"]) == ["api", "web"]
        assert sorted(seen) == ["api", "web"]

    def test_fan_out_probes_for_code_exactly_once(self, monkeypatch):
        # The answer is a property of the machine, not of a session: probing
        # per name would be one filesystem sweep each for one shared answer.
        probes: list[int] = []
        hints: list[bool | None] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "code_on_path", lambda: (probes.append(1), True)[1])
        monkeypatch.setattr(
            psmux,
            "decorate_session",
            lambda n, psmux=None, code_hint=None: hints.append(code_hint),
        )
        psmux.decorate_sessions(["api", "web", "docs"])
        assert len(probes) == 1
        assert hints == [True, True, True]

    def test_fan_out_passes_an_explicit_hint_through_without_probing(self, monkeypatch):
        hints: list[bool | None] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")

        def _boom():
            raise AssertionError("must not probe when the caller already knows")

        monkeypatch.setattr(psmux, "code_on_path", _boom)
        monkeypatch.setattr(
            psmux,
            "decorate_session",
            lambda n, psmux=None, code_hint=None: hints.append(code_hint),
        )
        psmux.decorate_sessions(["api", "web"], code_hint=False)
        assert hints == [False, False]

    def test_fan_out_without_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.decorate_sessions(["api"]) == []
