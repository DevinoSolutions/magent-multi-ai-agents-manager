"""reap._park: stop -> console-checked reset -> parked record, written LAST.

Nothing real is reached. ``_stop`` is patched, so no process table is walked;
the re-walk is a fake ``psmux.idle_sessions`` and the typing a fake
``fleet.paste_and_enter``. One ``events`` list records every step in the order
_park takes it, so each test pins the order as well as the steps."""

from __future__ import annotations

import dataclasses

import pytest

from magent import agent_state, reap
from magent.sessions import AGENT_TOOLS, agent_image_names
from tests.conftest import FakePlatform

_SID = "11111111-2222-3333-4444-555555555555"
_NOTICE = (
    "magent: parked after 120 min idle to free memory. "
    f"Resume: claude --resume {_SID}  (or magent status, r<n>)"
)
_MB = 1024 * 1024


def _sig(**over: object) -> reap.Signals:
    base: dict[str, object] = {
        "psmux_session": "demo",
        "session_id": _SID,
        "tool": "claude",
        "cmd": "claude --continue",
        "tree": (("pwsh.exe", 500, 1), ("claude.exe", 1000, 500)),
        "agent_pid": 1000,
        "agent_created": 123,
        "agent_image": "claude.exe",
        "agent_start": 0.0,
        "cwd": "/projects/demo",
        "in_scope": True,
        "shares_cwd": False,
        "tree_known": True,
        "root_is_shell": True,
        "same_logon_session": True,
        "agent_unreadable": False,
        "agent_count": 1,
        "image_is_agent": True,
        "cwd_matches": True,
        "claude_status": "idle",
        "claude_status_ts": 1000.0,
        "record_unreadable": False,
        "record_present": True,
        "record_state": "done",
        "record_session_id": _SID,
        "record_ts": 2000.0,
        "transcript_present": True,
        "transcript_mtime": 3000.0,
        "pane_state": "idle",
        "draft": "",
        "now": 10_000.0,
        "threshold_s": 7200.0,
    }
    base.update(over)
    return reap.Signals(**base)


_TOOLS = {"claude": AGENT_TOOLS["claude"]}


class _World:
    """One park's fake world. ``stop`` is what the patched _stop returns.
    ``walks`` scripts the re-walk, one answer per call (True = idle); the last
    answer repeats once the script is spent. The clock reads 0.0, 1.0, 2.0, ...
    -- one step per call -- and a sleep only records itself."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stop: reap.StopResult | None = None,
        walks: tuple[bool, ...] = (True,),
        sent: bool = True,
        reset: str | None = "RESET",
    ) -> None:
        self.events: list[tuple[object, ...]] = []
        self.stop_result = stop or reap.StopResult(
            [1001, 1000], [], 4 * _MB, None, False
        )
        self.walks = list(walks)
        self.sent = sent
        self.plat = FakePlatform(pane_reset=reset)
        self.ticks = 0
        monkeypatch.setattr(reap, "_stop", self._stop)
        monkeypatch.setattr("magent.psmux.idle_sessions", self._idle_sessions)
        monkeypatch.setattr("magent.fleet.paste_and_enter", self._paste_and_enter)

    def _stop(self, sig: reap.Signals, **_kw: object) -> reap.StopResult:
        self.events.append(("stop", sig.agent_pid))
        return self.stop_result

    def _idle_sessions(
        self,
        names: list[str],
        psmux: str | None = None,
        *,
        images: frozenset[str] | None = None,
        **_kw: object,
    ) -> set[str]:
        idle = self.walks.pop(0) if len(self.walks) > 1 else self.walks[0]
        self.events.append(("walk", tuple(names), psmux, images, idle))
        return set(names) if idle else set()

    def _paste_and_enter(
        self, name: str, text: str, *, psmux_bin: str | None = None, **_kw: object
    ) -> bool:
        self.events.append(("type", name, text, psmux_bin))
        return self.sent

    def writer(self, cwd: str, state: str, sid: str | None) -> None:
        self.events.append(("record", cwd, state, sid))

    def monotonic(self) -> float:
        now = float(self.ticks)
        self.ticks += 1
        return now

    def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))

    def park(self, sig: reap.Signals | None = None, **kw: object) -> reap.ParkResult:
        return reap._park(
            self.plat,
            sig or _sig(),
            tools=kw.pop("tools", _TOOLS),
            writer=kw.pop("writer", self.writer),
            monotonic=self.monotonic,
            sleep=self.sleep,
            **kw,
        )

    def kinds(self) -> list[object]:
        return [event[0] for event in self.events]


class TestTheOrder:
    def test_stop_then_reset_then_the_parked_record_last(self, monkeypatch):
        world = _World(monkeypatch)
        result = world.park()
        assert world.kinds() == ["stop", "walk", "type", "record"]
        assert world.events[2] == ("type", "demo", "RESET", None)
        assert world.events[3] == ("record", "/projects/demo", agent_state.PARKED, _SID)
        assert result == reap.ParkResult("demo", True, 4 * _MB, None)

    def test_the_reset_waits_for_an_idle_rewalk_and_is_typed_once(self, monkeypatch):
        # The cmd wrapper outlives the agent by a moment: the first re-walk
        # reads not idle, the second idle. One keystroke batch, after the second.
        world = _World(monkeypatch, walks=(False, True))
        world.park()
        assert world.kinds() == ["stop", "walk", "sleep", "walk", "type", "record"]
        assert [e[-1] for e in world.events if e[0] == "walk"] == [False, True]
        assert ("sleep", reap._RESET_POLL_S) in world.events

    def test_a_pane_never_idle_gets_no_keystroke_and_is_still_parked(
        self, monkeypatch, caplog
    ):
        # The clock steps 1s per read: re-walks at 0..4, and the read at 5
        # reaches RESET_SETTLE_S -- no sixth re-walk, nothing typed.
        world = _World(monkeypatch, walks=(False,))
        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop", *["walk", "sleep"] * 4, "walk", "record"]
        assert result.parked is True
        assert [r.getMessage() for r in caplog.records] == [
            "reap: pane demo not proven idle after the kill; leaving it unreset"
        ]


class TestNothingIsParkedUnlessTheStopVerified:
    def test_a_stop_guard_parks_nothing(self, monkeypatch, caplog):
        world = _World(
            monkeypatch, stop=reap.StopResult([], [], 0, "root-identity", True)
        )
        with caplog.at_level("ERROR", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop"]
        assert world.plat.pane_resets == []
        assert result == reap.ParkResult("demo", False, 0, "abort:root-identity")
        assert "root-identity" in caplog.text

    def test_a_surviving_agent_root_parks_nothing(self, monkeypatch, caplog):
        world = _World(monkeypatch, stop=reap.StopResult([1001], [1000], 7, None, True))
        with caplog.at_level("ERROR", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop"]
        assert world.plat.pane_resets == []
        assert result == reap.ParkResult("demo", False, 7, "root-survived")
        assert "survived" in caplog.text

    def test_survivors_other_than_the_root_still_park(self, monkeypatch, caplog):
        world = _World(
            monkeypatch, stop=reap.StopResult([1000], [1001], 1, None, False)
        )
        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop", "walk", "type", "record"]
        assert result.parked is True
        assert "1 process(es) survived" in caplog.text


class TestTheReset:
    def test_it_is_built_for_the_pane_shell_with_the_notice(self, monkeypatch):
        world = _World(monkeypatch)
        world.park()
        assert world.plat.pane_resets == [("pwsh.exe", _NOTICE)]

    @pytest.mark.parametrize(
        ("tool", "cmd", "resume"),
        [
            (
                "claude",
                "claude --model opus --continue",
                f"claude --model opus --resume {_SID}",
            ),
            ("codex", "codex", f"codex resume {_SID}"),
            ("aider", "aider --yes", "aider --yes"),  # no resume: the command itself
        ],
    )
    def test_the_notice_names_the_resume_command(self, monkeypatch, tool, cmd, resume):
        world = _World(monkeypatch)
        world.park(_sig(tool=tool, cmd=cmd, threshold_s=1830.0))
        notice = world.plat.pane_resets[0][1]
        assert notice == (
            "magent: parked after 30 min idle to free memory. "
            f"Resume: {resume}  (or magent status, r<n>)"
        )

    # The notice is TYPED into the pane, so a control character in it is a
    # keystroke: a CR submits early, a tab completes, an ESC starts a sequence.
    # Both ends of C0, DEL and both ends of C1 -- every Cc code point range.
    @pytest.mark.parametrize(
        "ctrl", ["\x00", "\t", "\r", "\x1b", "\x1f", "\x7f", "\x80", "\x9b", "\x9f"]
    )
    def test_a_control_character_leaves_the_resume_command_off_the_notice(
        self, monkeypatch, caplog, ctrl
    ):
        world = _World(monkeypatch)
        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park(_sig(cmd=f"claude --name a{ctrl}b --continue"))
        notice = world.plat.pane_resets[0][1]
        assert notice == (
            "magent: parked after 120 min idle to free memory. "
            "Resume: magent status, r<n>  "
            "(the resume command holds a control character, see reap.log)"
        )
        assert result.parked is True
        # The command reaches the log escaped, never raw.
        assert f"claude --name a{repr(ctrl)[1:-1]}b --resume {_SID}" in caplog.text
        assert ctrl not in caplog.text
        assert "demo" in caplog.text
        assert [r.levelname for r in caplog.records] == ["WARNING"]

    def test_a_control_character_in_the_session_id_is_caught_too(
        self, monkeypatch, caplog
    ):
        world = _World(monkeypatch)
        with caplog.at_level("WARNING", logger="magent.reap"):
            world.park(_sig(session_id=f"{_SID}\r"))
        assert "Resume: magent status, r<n>" in world.plat.pane_resets[0][1]
        assert f"--resume {_SID}\\r" in caplog.text

    # Only a control character is a keystroke: accented letters, typographic
    # quotes, a no-break space and a zero-width space (a format character, Cf,
    # not Cc) are carried verbatim, with nothing logged.
    @pytest.mark.parametrize(
        "text", ["caf\u00e9", "\u2019q\u2019", "a\u00a0b", "a\u200bb", "a b"]
    )
    def test_other_text_stays_verbatim_and_logs_nothing(
        self, monkeypatch, caplog, text
    ):
        world = _World(monkeypatch)
        with caplog.at_level("WARNING", logger="magent.reap"):
            world.park(_sig(cmd=f"claude --name {text} --continue"))
        assert world.plat.pane_resets[0][1] == (
            "magent: parked after 120 min idle to free memory. "
            f"Resume: claude --name {text} --resume {_SID}  (or magent status, r<n>)"
        )
        assert caplog.records == []

    def test_the_rewalk_asks_with_the_sweeps_own_images_and_psmux(self, monkeypatch):
        standin = dataclasses.replace(AGENT_TOOLS["claude"], images=("standin",))
        tools = {"claude": AGENT_TOOLS["claude"], "standin": standin}
        world = _World(monkeypatch)
        world.park(tools=tools, psmux_bin="C:/bin/psmux.exe")
        walk = world.events[1]
        assert walk == (
            "walk",
            ("demo",),
            "C:/bin/psmux.exe",
            agent_image_names(tools),
            True,
        )
        assert "standin" in agent_image_names(tools)
        assert world.events[2] == ("type", "demo", "RESET", "C:/bin/psmux.exe")

    def test_a_shell_with_no_reset_line_is_left_alone_and_still_parked(
        self, monkeypatch, caplog
    ):
        world = _World(monkeypatch, reset=None)
        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop", "record"]
        assert result.parked is True
        assert "pwsh.exe" in caplog.text

    def test_a_failed_send_is_a_warning_and_still_parked(self, monkeypatch, caplog):
        world = _World(monkeypatch, sent=False)
        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop", "walk", "type", "record"]
        assert result.parked is True
        assert "could not type" in caplog.text

    def test_a_rewalk_that_raises_types_nothing_and_still_records(
        self, monkeypatch, caplog
    ):
        # Unknown is not idle. The agent is already dead: a failed re-walk must
        # not cost the record that names its session.
        world = _World(monkeypatch)

        def boom(*_a: object, **_k: object) -> set[str]:
            world.events.append(("walk-raised",))
            raise RuntimeError("console helper crashed")

        monkeypatch.setattr("magent.psmux.idle_sessions", boom)
        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park()
        assert world.kinds() == ["stop", "walk-raised", "record"]
        assert result.parked is True
        assert "console helper crashed" in caplog.text


class TestTheRecord:
    def test_a_failed_write_is_a_warning_and_the_park_still_counts(
        self, monkeypatch, caplog
    ):
        world = _World(monkeypatch)

        def refuse(*_a: object) -> None:
            raise OSError("disk full")

        with caplog.at_level("WARNING", logger="magent.reap"):
            result = world.park(writer=refuse)
        assert result == reap.ParkResult("demo", True, 4 * _MB, None)
        assert "disk full" in caplog.text

    def test_the_default_writer_is_the_state_store(self, monkeypatch):
        # conftest points agent_state.STATE_DIR at tmp.
        world = _World(monkeypatch)
        reap._park(
            world.plat,
            _sig(),
            tools=_TOOLS,
            monotonic=world.monotonic,
            sleep=world.sleep,
        )
        rec = agent_state.state_for("/projects/demo")
        assert rec is not None
        assert rec["state"] == agent_state.PARKED
        assert rec["session_id"] == _SID

    def test_one_info_line_names_what_was_parked(self, monkeypatch, caplog):
        world = _World(monkeypatch)
        with caplog.at_level("INFO", logger="magent.reap"):
            world.park()
        lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
        assert lines == [
            (
                f"reap: parked demo (session {_SID!r}, pid 1000/claude.exe created"
                " 123) idle~7000s killed=2 survivors=0 freed~4MB"
            )
        ]

    def test_the_info_line_escapes_the_session_id(self, monkeypatch, caplog):
        # The id is read from a file magent does not write: a raw CR or LF in
        # it would forge a log line of its own.
        sid = f"{_SID}\r\nreap: parked nothing"
        world = _World(monkeypatch)
        with caplog.at_level("INFO", logger="magent.reap"):
            world.park(_sig(session_id=sid))
        lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
        assert len(lines) == 1
        assert f"(session {sid!r}, " in lines[0]
        assert "\r" not in lines[0]
        assert "\n" not in lines[0]
