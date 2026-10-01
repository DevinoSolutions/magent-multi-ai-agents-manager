"""Stand-in for Claude Code on a node, for tests/e2e/test_nodes_real.py.

Not a test module (no ``test_`` prefix). The rig commits it into the test
repo as ``.magent-e2e/node_agent.py``, next to a byte copy of
``src/magent/sessions/claude.py`` saved as ``claude_encoding.py``, and the
node's ``claude`` stub (``tests/e2e/_claude_stub.sh``) execs it from the
pane's working directory. So the node needs no magent install, and the
transcript path comes from the product's ONE encoder (DECISION-11a/f), never
a restated one: a restated regex already drifted once, by keeping ``_``.

It imitates the parts of Claude Code's contract that the nodes feature reads:

* how it starts. ``--resume <id>`` with no such transcript prints ``No
  conversation found with session ID: <id>`` and exits 1. ``--continue`` with
  no transcript prints ``No conversation found to continue`` and exits 1,
  which is why a first bring-up starts the fresh form and why bring_up.sh
  re-checks the session after starting it (DECISION-11c). Neither flag is a
  fresh session with a new id.
* its transcript: ``~/.claude/projects/<enc(realpath cwd)>/<sid>.jsonl``,
  one line per prompt, shaped like Claude Code's (``type``, ``sessionId``,
  ``cwd``, ``message``). Like the real one, it writes nothing there until
  the first prompt.
* its hooks: every ``command`` hook in ``~/.claude/settings.json`` for
  SessionStart, UserPromptSubmit and Stop, run through a shell with the
  event on stdin, as Claude Code runs them.
* a screen: ``NODE-READY <sid> <mode>``, then one ``MARK-<tok>`` per
  ``poke <tok>`` line and one ``PUSHED-<sha>`` per ``commit <tok>`` line.
  ``exit`` (or end of input) exits 0.

Everything it records about itself goes to ``~/.magent-e2e/agent-log.jsonl``
in the NODE USER's home, never into the repo: a stray file there would dirty
the node's tree, and the next bring-up would refuse it. It records variable
NAMES only, never values. Needles carry no spaces and no doubled characters,
because tmux redraws a reattached pane with cursor motion, which splits
either. It exits by itself after ``--max-seconds``, so a lost teardown cannot
leave it running for the rest of the job.
"""

from __future__ import annotations

import json
import os
import queue
import runpy
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

READY = "NODE-READY"
# Variables whose PRESENCE in the pane would mean the PC's environment
# crossed the wire. remote_mux forwards none; spawn_child_env is for local
# panes only. Names are recorded, never values.
CANARIES = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "NO_COLOR",
    "FORCE_COLOR",
    "ANTHROPIC_API_KEY",
    "GH_TOKEN",
)
HOOK_TIMEOUT_S = 10
DEFAULT_MAX_SECONDS = 900.0
_ENCODING = Path(__file__).resolve().with_name("claude_encoding.py")


def _say(text: str, *, end: str = "\n") -> None:
    sys.stdout.write(text + end)
    sys.stdout.flush()


def _encode(path: str) -> str:
    """The product's encoder, loaded from the byte copy beside this file.
    ``run_path`` compiles in memory, so no ``__pycache__`` lands in the clone."""
    return str(runpy.run_path(str(_ENCODING))["encode_claude_project_path"](path))


def _log(record: dict[str, object]) -> None:
    folder = Path.home() / ".magent-e2e"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "agent-log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _parse(argv: list[str]) -> tuple[str | None, bool, float]:
    """``(resume id, continue, max seconds)``. Anything else a configured
    command passes (a permission flag, say) is accepted and ignored."""
    resume: str | None = None
    cont = False
    max_s = DEFAULT_MAX_SECONDS
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--resume", "-r") and i + 1 < len(argv):
            resume = argv[i + 1]
            i += 1
        elif arg.startswith("--resume="):
            resume = arg.split("=", 1)[1]
        elif arg in ("--continue", "-c"):
            cont = True
        elif arg == "--max-seconds" and i + 1 < len(argv):
            max_s = float(argv[i + 1])
            i += 1
        i += 1
    return resume, cont, max_s


def _hooks(event: str) -> list[str]:
    """The command strings configured for ``event``, in file order."""
    path = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    out: list[str] = []
    for group in groups if isinstance(groups, list) else []:
        inner = group.get("hooks") if isinstance(group, dict) else None
        for hook in inner if isinstance(inner, list) else []:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
            ):
                out.append(hook["command"])
    return out


def _fire(event: str, sid: str, cwd: str, **extra: object) -> None:
    """Run ``event``'s hooks. ``shell=True`` is deliberate: Claude Code runs a
    hook command through a shell, and these strings come from the
    settings.json that provisioning wrote, never from test input."""
    payload = json.dumps(
        {"session_id": sid, "cwd": cwd, "hook_event_name": event, **extra}
    ).encode("utf-8")
    for command in _hooks(event):
        try:
            done = subprocess.run(
                command,
                shell=True,
                input=payload,
                capture_output=True,
                timeout=HOOK_TIMEOUT_S,
                check=False,
            )
            rc: int | None = done.returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = None
        _log({"event": "hook", "hook": event, "rc": rc, "session_id": sid})


def _stdin_lines() -> queue.Queue[str | None]:
    """Lines from stdin on a daemon thread, so the ``--max-seconds`` guard can
    fire while no input arrives. ``None`` is end of input."""
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                lines.put(None)
                return
            lines.put(line.rstrip("\r\n"))

    threading.Thread(target=pump, daemon=True).start()
    return lines


def _commit(tok: str, cwd: str) -> str:
    """Commit and push one file: the tree stays clean, as a real agent's
    committed work leaves it."""
    Path(cwd, f"work-{tok}.txt").write_text(f"{tok}\n", encoding="utf-8")
    ident = ["-c", "user.name=magent-e2e", "-c", "user.email=e2e@magent.invalid"]
    for args in (
        ["add", f"work-{tok}.txt"],
        [*ident, "commit", "-q", "-m", f"e2e-{tok}"],
        ["push", "-q", "origin", "HEAD"],
    ):
        subprocess.run(["git", *args], cwd=cwd, check=True, timeout=60)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return head.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    resume, cont, max_s = _parse(args)
    cwd = os.path.realpath(os.getcwd())
    tdir = Path.home() / ".claude" / "projects" / _encode(cwd)
    if resume is not None:
        if not (tdir / f"{resume}.jsonl").is_file():
            _say(f"No conversation found with session ID: {resume}")
            return 1
        mode, sid = "resume", resume
    elif cont:
        found = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not found:
            _say("No conversation found to continue")
            return 1
        mode, sid = "continue", found[-1].stem
    else:
        mode, sid = "fresh", str(uuid.uuid4())

    _log(
        {
            "event": "start",
            "argv": args,
            "mode": mode,
            "session_id": sid,
            "cwd": cwd,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "home": os.environ.get("HOME", ""),
            "lang": os.environ.get("LANG", ""),
            "canaries_present": sorted(n for n in CANARIES if n in os.environ),
        }
    )
    _fire("SessionStart", sid, cwd, source="resume" if mode != "fresh" else "startup")
    _say(f"{READY} {sid} {mode}")

    lines = _stdin_lines()
    deadline = time.monotonic() + max_s
    while True:
        _say("> ", end="")
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            _log({"event": "max-seconds", "session_id": sid})
            return 0
        if line is None or line.strip() == "exit":
            _log({"event": "exit", "session_id": sid})
            return 0
        verb, _, tok = line.strip().partition(" ")
        if verb == "poke" and tok:
            tdir.mkdir(parents=True, exist_ok=True)
            record = {
                "type": "user",
                "sessionId": sid,
                "cwd": cwd,
                "message": {"role": "user", "content": line.strip()},
            }
            with (tdir / f"{sid}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            _fire("UserPromptSubmit", sid, cwd, prompt=line.strip())
            _log({"event": "poke", "tok": tok, "session_id": sid})
            _say(f"MARK-{tok}")
            _fire("Stop", sid, cwd, background_tasks=[])
        elif verb == "commit" and tok:
            _say(f"PUSHED-{_commit(tok, cwd)}")


if __name__ == "__main__":
    raise SystemExit(main())
