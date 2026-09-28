"""The harness of the nodes e2e tier (tests/e2e/test_nodes_real.py).

Not a test module (no ``test_`` prefix). A "node" here is the CI runner
itself, reached through the loopback sshd that
``.github/actions/setup-ssh-server`` provisions, as a DISPOSABLE Linux user
the harness creates over a root hop and deletes afterwards. Nothing on the
node's side of the wire is faked: bash, tmux, git, ssh and python3 are the
runner's own. The one substitution there is ``claude`` -- the stub the
nodes-e2e workflow installs as ``/usr/local/bin/claude``, which runs the
stand-in agent (``_node_agent.py``) the test repo commits beside its code.
The PC side is ``python -m magent`` children under a tmp HOME, with a
recording ``psmux`` shim first on PATH for the negative pins.

Law: substitute only at a boundary, and add nothing to the product. Every
node-side fact is read through raw ssh as the node user, never through
magent, and never through the local filesystem.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from tests.e2e._pty import Budget

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from magent.nodes import Node

NICK = "loop"
# One wall clock for a module's node stages (see _pty.Budget). The healthy D
# path, rig build through D16, measured 92 s on the hosted ubuntu-latest
# runner (PR #239 at 2a55f47); the job's timeout-minutes sits well above
# this, so a slow stage lands as a FAILURE with its output, never a cancel.
NODES_BUDGET_S = 300.0
# How long a stage waits for the node sync daemon to come up (`node sync
# -d`, serve's supervisor, a bring-up): two cold interpreters in a row, the
# spawner and the daemon, and one daemon import alone was measured at
# 2.5-13 s on a loaded desktop (cli/node_cmd._START_POLLS). It stays under
# serve's supervise interval, so a daemon serve started is its FIRST
# check's: a failed first check cannot be rescued by the next one.
DAEMON_START_S = 30.0
# The least a stage gets while any budget remains (see clamp).
STAGE_FLOOR_S = 5.0
# Teardown gets its own allowance, whatever the module's budget has left: a
# user that is never deleted is a leak on the runner, and the timeouts below
# must still fire when the module budget is spent.
CLEANUP_TIMEOUT_S = 60.0
# One diag() call: its reads share DIAG_S while the module budget lasts and
# DIAG_FLOOR_S after it (a failure is usually read after the budget ran out),
# each read at most DIAG_READ_S. A read with less than DIAG_MIN_READ_S left is
# skipped, named: no ssh round trip fits in less, and on a coarse clock
# (Windows' 15.6 ms monotonic tick before Python 3.13) the allowance's last
# crumbs were each "tried" and timed out at once, seven reads for three.
DIAG_S = 30.0
DIAG_FLOOR_S = 10.0
DIAG_READ_S = 10.0
DIAG_MIN_READ_S = 1.0
# Lines of each log and of the pane a diag() shows.
DIAG_TAIL = 80
GATE_VAR = "MDTEST_NODES_REAL"
SSH_VARS = ("MDTEST_SSH_PORT", "MDTEST_SSH_KEY", "MDTEST_SSH_HOST")
STUB = Path("/usr/local/bin/claude")
# What the nodes-e2e workflow installs as STUB, byte for byte.
STUB_SRC = Path(__file__).with_name("_claude_stub.sh")
# The only hostnames the node may resolve to: the rig runs useradd, userdel
# and pkill as root there, so it must be this machine's loopback sshd.
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
AGENT_DIR = ".magent-e2e"
AGENT_SRC = Path(__file__).with_name("_node_agent.py")
# CRLF and UTF-8: tar and the 0600 copy must carry these bytes untouched.
ENV_BYTES = b"API_KEY=e2e-\xc3\xa9t\xc3\xa9\r\nSECOND=two\r\n"
# The directory the local clones live in: a space, an `&` (the measured
# psmux/pwsh hazard from the fleet tier) and a non-ASCII letter.
AWKWARD_DIR = "pc dir & ü"

# Every isolation pin a PC-side child carries, by the laws in CLAUDE.md. Only
# the names that are FIELDS of magent.env.MagentEnv on this branch are set:
# MagentEnv is extra="forbid", so an unknown MAGENT_* fails every CLI call at
# startup (MAGENT_MCP_RELAY and MAGENT_ACCOUNT_ROUTING exist on other lines).
PIN_VALUES: dict[str, str] = {
    "MAGENT_HOTKEY_SUPERVISOR": "0",
    "MAGENT_UPLOAD_SUPERVISOR": "0",
    "MAGENT_PSMUX_BOOST": "0",
    "MAGENT_NODE_SYNC": "0",
    "MAGENT_SESSION0_POLICY": "allow",
    "MAGENT_MCP_RELAY": "0",
    "MAGENT_ACCOUNT_ROUTING": "0",
}
# The pins this tier cannot run without: a schema that lost one of them is a
# red test, not a quietly dropped pin.
REQUIRED_PINS = (
    "MAGENT_HOTKEY_SUPERVISOR",
    "MAGENT_UPLOAD_SUPERVISOR",
    "MAGENT_PSMUX_BOOST",
    "MAGENT_SESSION0_POLICY",
    "MAGENT_NODE_SYNC",
)
# Set in every PC-side child, and never allowed to reach a node pane
# (remote_mux forwards no environment). MAGENT_* canaries are impossible:
# MagentEnv would refuse them.
CANARIES: dict[str, str] = {
    "NO_COLOR": "1",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "ANTHROPIC_API_KEY": "e2e-canary",
}
_STRIPPED = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CLAUDECODE",
        "CLAUDE_PID",
    }
)
_STRIPPED_PREFIXES = ("MAGENT_", "ANTHROPIC_", "CLAUDE_CODE_")
# A disposable node user's name, as a bash `case` pattern: the root hop
# re-checks it before any useradd or delete, so a teardown can only ever
# remove a user this shape (and, past that, only one carrying this run's
# stamp -- see _DELETE_USER).
_USER_CASE = "mgn[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"


def schema_pins() -> set[str]:
    """``MAGENT_<FIELD>`` for every field of this branch's MagentEnv."""
    from magent.env import MagentEnv

    return {f"MAGENT_{name.upper()}" for name in MagentEnv.model_fields}


def child_env(
    pc_home: Path, *, sync: bool = False, extra_path: Sequence[Path] = ()
) -> dict[str, str]:
    """The environment of a PC-side ``magent`` child.

    ``os.environ`` minus every MAGENT_* (the user's Sentry DSN included), the
    gh tokens, the Anthropic and Claude harness variables; the whole HOME
    family and XDG_CONFIG_HOME at ``pc_home``; the pins that exist in the
    schema; the canaries; ``extra_path`` first on PATH. ``sync`` flips
    MAGENT_NODE_SYNC on, for the legs that exercise the sync supervisor."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in _STRIPPED and not k.upper().startswith(_STRIPPED_PREFIXES)
    }
    home = str(pc_home)
    drive, tail = os.path.splitdrive(home)
    env.update(
        HOME=home,
        USERPROFILE=home,
        HOMEDRIVE=drive,
        HOMEPATH=tail or os.sep,
        GH_CONFIG_DIR=str(pc_home / "gh"),
        # Linux config lookups go through XDG before HOME: the runner's own
        # would otherwise pass straight through.
        XDG_CONFIG_HOME=str(pc_home / ".config"),
    )
    if sys.platform == "win32":
        env.update(
            APPDATA=str(pc_home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(pc_home / "AppData" / "Local"),
        )
    known = schema_pins()
    env.update({k: v for k, v in PIN_VALUES.items() if k in known})
    if "MAGENT_NODE_SYNC" in known:
        env["MAGENT_NODE_SYNC"] = "1" if sync else "0"
    env.update(CANARIES)
    if extra_path:
        env["PATH"] = os.pathsep.join(
            [*(str(p) for p in extra_path), env.get("PATH", "")]
        )
    return env


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Wire:
    """The CI loopback sshd, as setup-ssh-server exports it."""

    port: str
    key: Path
    host: str


def ssh_wire_or_skip() -> Wire:
    """The loopback sshd's coordinates, or a clean skip where there is none
    (every dev box). The client-only pins ride this gate alone."""
    port, key, host = (os.environ.get(name) for name in SSH_VARS)
    if not port or not key or not host:
        pytest.skip(
            "live SSH server not configured (setup-ssh-server exports "
            f"{'/'.join(SSH_VARS)} in CI; local runs skip)"
        )
    return Wire(port=port, key=Path(key), host=host)


def node_wire_or_skip() -> Wire:
    """The gate of every node-hosting test: ``MDTEST_NODES_REAL=1`` (set only
    by the nodes-e2e workflow). Once it is on, a missing piece of the node is
    a provisioning bug and FAILS -- a runner without tmux, the stub or the
    ssh wire must not read as coverage (the fleet-tier rule). So does a node
    that is not this machine, or a stub that is not ours: "T1 is loopback
    only" is checked, not assumed."""
    if os.environ.get(GATE_VAR) != "1":
        # No ci.yml selection reaches these tests (theirs all require the e2e
        # marker, which the journey lacks): only the nodes-e2e job collects
        # them there, so a CI run without the gate is a lost or misspelled
        # variable -- which must not read green with zero node coverage.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(
                f"node-hosting tier selected on CI without {GATE_VAR}=1: the "
                "nodes-e2e workflow step lost (or misspelled) its gate variable"
            )
        pytest.skip(
            f"node-hosting tier: gated on {GATE_VAR}=1 (the nodes-e2e workflow "
            "sets it on a CI runner; never set it on a dev box)"
        )
    missing = [name for name in SSH_VARS if not os.environ.get(name)]
    if missing:
        pytest.fail(
            f"{GATE_VAR}=1 but {', '.join(missing)} unset: setup-ssh-server did "
            "not run or failed, so there is no node to test"
        )
    if sys.platform != "linux":
        pytest.fail(
            f"{GATE_VAR}=1 on {sys.platform}: a node is a Linux box; the "
            "node-hosting tier runs on the ubuntu runner only"
        )
    for tool in ("ssh", "tmux", "git", "python3", "ssh-keygen", "ssh-keyscan"):
        if shutil.which(tool) is None:
            pytest.fail(f"{tool} not on PATH on the node runner: a provisioning bug")
    if not STUB.is_file():
        pytest.fail(
            f"{STUB} is missing: the nodes-e2e workflow installs "
            "tests/e2e/_claude_stub.sh there before this tier runs"
        )
    if STUB.read_bytes() != STUB_SRC.read_bytes():
        pytest.fail(
            f"{STUB} is not tests/e2e/_claude_stub.sh: the node would run "
            "some other claude than the stand-in"
        )
    wire = ssh_wire_or_skip()
    hostname = ssh_config_hostname(wire.host)
    if hostname not in LOOPBACK:
        pytest.fail(
            f"{SSH_VARS[2]}={wire.host} resolves to {hostname!r}, not this "
            "machine's loopback: the rig creates and deletes users as root on "
            "the node, so it runs against the loopback sshd only"
        )
    return wire


def no_monitors(run: Run) -> NoReturn:
    """D6's ``--go --dry-run`` stopped at "No monitors detected": run_magent
    plans the monitor grid before it reads a project, so with no screen the
    preview never reaches the node rows. The nodes-e2e job provisions one
    (setup-virtual-displays), so on CI a missing screen is a provisioning bug
    and FAILS -- a regression there must not bring back the green skip that
    left D6 unexercised. Anywhere else (a container with the gate on) it
    skips."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail(
            "--go --dry-run found no monitors on the nodes-e2e runner: "
            "setup-virtual-displays did not take, so the launch preview went "
            f"unchecked\n{run.show()}"
        )
    pytest.skip("no monitor: --go --dry-run stops at 'No monitors detected'")


def resolved_hostname(ssh_g: str) -> str | None:
    """The ``hostname`` ``ssh -G`` resolved (lowercased), or None."""
    for line in ssh_g.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "hostname":
            return value.strip().lower()
    return None


def ssh_config_hostname(host: str) -> str | None:
    """Where ssh would dial for ``host``, after the mdssh alias: ``ssh -G``
    reads the config and connects to nothing."""
    try:
        done = subprocess.run(
            ["ssh", "-G", host],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return resolved_hostname(done.stdout) if done.returncode == 0 else None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def clamp(budget: Budget, want: float, tag: str) -> float:
    """A stage timeout: ``want``, or what is left of ``budget``, floored at
    ``STAGE_FLOOR_S`` while any budget remains -- a slow first python start
    still gets to fail with its output. Once the budget is spent the stage
    FAILS here, naming itself: the floor is not granted again to every stage
    after the deadline."""
    if budget.remaining() <= 0:
        pytest.fail(f"budget exhausted before {tag}")
    return max(STAGE_FLOOR_S, budget.clamp(want))


def token(n: int = 6) -> str:
    """Lowercase letters with no character repeated back to back: pane needles
    are matched in a tmux-redrawn stream, where a run of one character may be
    drawn as a repeat escape."""
    out: list[str] = []
    while len(out) < n:
        c = secrets.choice("abcdefghijkmnpqrstuvwxyz")
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


@dataclass(frozen=True)
class Run:
    argv: tuple[str, ...]
    rc: int
    out: str
    err: str

    @property
    def said(self) -> str:
        return self.out + self.err

    def show(self) -> str:
        return (
            f"$ {shlex.join(self.argv)}\nrc={self.rc}\n"
            f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}"
        )


def run_files(
    argv: Sequence[str],
    out_dir: Path,
    tag: str,
    timeout: float,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    stdin: bytes | None = None,
) -> Run:
    """Run a child to completion, its output in FILES, never a pipe.

    A tmux server or a detached daemon the child starts inherits its stdout;
    a captured pipe would keep ``subprocess.run`` waiting for that survivor's
    whole lifetime. Stdin is a file too (or /dev/null): an ssh must never read
    the test runner's own stdin. A timeout fails the test with what the child
    said before it was killed."""
    stamp = f"{tag}-{time.monotonic_ns()}"
    out_p, err_p = out_dir / f"{stamp}.stdout", out_dir / f"{stamp}.stderr"
    in_p = out_dir / f"{stamp}.stdin"
    in_p.write_bytes(stdin or b"")
    timed_out = False
    rc = -1
    with (
        in_p.open("rb") as fi,
        out_p.open("wb") as fo,
        err_p.open("wb") as fe,
    ):
        try:
            rc = subprocess.run(
                list(argv),
                stdin=fi,
                stdout=fo,
                stderr=fe,
                timeout=timeout,
                env=env,
                cwd=cwd,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    run = Run(
        argv=tuple(argv),
        rc=rc,
        out=out_p.read_bytes().decode("utf-8", "replace"),
        err=err_p.read_bytes().decode("utf-8", "replace"),
    )
    if timed_out:
        pytest.fail(f"{tag}: timed out after {timeout:.0f}s\n{run.show()}")
    return run


def wait_for(
    what: str,
    check: Callable[[], object],
    want: float,
    *,
    budget: Budget,
    explain: Callable[[], str] = lambda: "",
    interval: float = 0.5,
) -> object:
    """Poll ``check`` until it is truthy, or FAIL naming ``what`` and whatever
    ``explain`` adds. Bounded: ``want`` clamped to ``budget``, plus one check
    (itself clamped where it runs a child)."""
    timeout = clamp(budget, want, what)
    deadline = time.monotonic() + timeout
    while True:
        got = check()
        if got:
            return got
        if time.monotonic() >= deadline:
            pytest.fail(f"never happened within {timeout:.0f}s: {what}\n{explain()}")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------

# BatchMode: a prompt would hang the stage instead of failing it. The rest
# mirror remote_mux.SSH_BATCH_OPTS, with a connect bound a loaded runner's
# sshd can meet while it is also serving a bring-up.
SSH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
)


@dataclass
class Remote:
    """One login on the node (``user@host`` through the mdssh alias), for
    arranging and inspecting it independently of the product."""

    target: str
    out_dir: Path
    budget: Budget

    def run(self, argv: Sequence[str], *, tag: str, want: float = 60.0) -> Run:
        """``argv`` as ONE ``bash -c`` string (the product's DECISION-9 rule):
        only bash ever parses it, whatever the login shell is."""
        remote = "bash -c " + shlex.quote(shlex.join(argv))
        return run_files(
            ["ssh", *SSH_OPTS, self.target, remote],
            self.out_dir,
            f"ssh-{tag}",
            clamp(self.budget, want, f"ssh-{tag}"),
        )

    def script(
        self, text: str, *args: str, tag: str, want: float = 60.0, timeout: float = 0
    ) -> Run:
        """``text`` on stdin to ``bash -s -- <args>``. ``timeout`` overrides
        the budget clamp (teardown's reserved allowance)."""
        remote = shlex.join(["bash", "-s", "--", *args])
        return run_files(
            ["ssh", *SSH_OPTS, self.target, remote],
            self.out_dir,
            f"ssh-{tag}",
            timeout or clamp(self.budget, want, f"ssh-{tag}"),
            stdin=text.encode("utf-8"),
        )

    def must(self, argv: Sequence[str], *, tag: str, want: float = 60.0) -> str:
        run = self.run(argv, tag=tag, want=want)
        if run.rc != 0:
            pytest.fail(f"node command failed ({self.target}):\n{run.show()}")
        return run.out

    def read_bytes(self, path: str, *, tag: str) -> bytes | None:
        """A node file's exact bytes (base64 over the wire), or None when it
        does not exist."""
        run = self.run(["base64", "-w0", "--", path], tag=tag)
        if run.rc != 0:
            return None
        return base64.b64decode(run.out.strip())


# The GECOS field of every node user this harness creates: this prefix and a
# per-run token. The name only has the right SHAPE; the stamp is what proves
# this run made the user, and the delete refuses any user without it.
OWNER_PREFIX = "magent-e2e"
# _CREATE_USER's exits before useradd runs: nothing was created, so a failure
# with one of these deletes nothing (an existing user is someone else's).
_BAD_NAME, _EXISTS = 2, 3
# _DELETE_USER's exit for a user whose stamp is not this run's.
_NOT_OURS = 5

_CREATE_USER = f"""set -eu
u=$1
key=$2
owner=$3
case $u in {_USER_CASE}) ;; *) echo "refusing to create $u" >&2; exit {_BAD_NAME} ;; esac
if getent passwd "$u" >/dev/null; then echo "$u already exists" >&2; exit {_EXISTS}; fi
useradd --create-home --shell /bin/bash --comment "$owner" "$u"
# '*' is no password, not a LOCKED one ('!'): sshd refuses pubkey logins to a
# locked account when PAM is off.
usermod -p '*' "$u"
home=$(getent passwd "$u" | cut -d: -f6)
group=$(id -gn "$u")
install -d -m 700 -o "$u" -g "$group" "$home/.ssh"
printf '%s\\n' "$key" > "$home/.ssh/authorized_keys"
chown "$u:$group" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
printf '%s %s\\n' "$(id -u "$u")" "$home"
"""

# As the node user: its own key (a real node fetches origin with its own
# identity), self-authorized so it can fetch from the ssh origin in its own
# home, and the loopback host key trusted -- no prompt, no StrictHostKey
# relaxation on the node side.
_BOOTSTRAP_USER = """set -eu
port=$1
ssh-keygen -q -t ed25519 -N '' -C magent-e2e-node -f "$HOME/.ssh/id_ed25519"
# ssh-keygen's 0600 is umask 077 over an open(0644), and a default ACL on the
# parent overrides the umask: the hosted runner's homes carry ACLs, the key
# came out 0644 there, and ssh refused to load it for the clone.
chmod 600 "$HOME/.ssh/id_ed25519"
cat "$HOME/.ssh/id_ed25519.pub" >> "$HOME/.ssh/authorized_keys"
ssh-keyscan -p "$port" localhost > "$HOME/.ssh/known_hosts" 2>/dev/null || true
if [ ! -s "$HOME/.ssh/known_hosts" ]; then
  echo "ssh-keyscan found no host key on localhost:$port" >&2
  exit 4
fi
mkdir -p "$HOME/origin"
"""

# The ownership check runs first and needs only getent and bash builtins:
# nothing is killed, found or deleted before it has passed.
_DELETE_USER = f"""set -u
u=$1
owner=$2
case $u in {_USER_CASE}) ;; *) echo "refusing to delete $u" >&2; exit {_BAD_NAME} ;; esac
entry=$(getent passwd "$u") || exit 0
IFS=: read -r _ _ uid _ gecos home _ <<< "$entry"
if [ "$gecos" != "$owner" ]; then
  echo "refusing to delete $u: stamped '$gecos', not this run's" >&2
  exit {_NOT_OURS}
fi
loginctl terminate-user "$u" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6; do
  pkill -KILL -u "$uid" 2>/dev/null || true
  sleep 0.5
  pgrep -u "$uid" >/dev/null || break
done
rm -rf -- "/tmp/tmux-$uid"
find /tmp /var/tmp /dev/shm -xdev -uid "$uid" -depth -delete 2>/dev/null || true
for _ in 1 2 3 4 5 6; do
  userdel -r "$u" 2>/dev/null || true
  getent passwd "$u" >/dev/null || break
  pkill -KILL -u "$uid" 2>/dev/null || true
  sleep 0.5
done
case $home in /home/{_USER_CASE}) rm -rf -- "$home" ;; esac
if getent passwd "$u" >/dev/null; then echo "$u still exists" >&2; exit 1; fi
if [ -e "$home" ]; then echo "$home still exists" >&2; exit 1; fi
"""


@dataclass
class NodeUser:
    """A disposable node user, created and deleted over the root hop -- the
    end state ``magent node setup`` (plan F) produces, done by the harness
    until F lands on this line (then F1 drives the product's own setup)."""

    name: str
    # The GECOS stamp this run wrote (OWNER_PREFIX + a token): the delete
    # refuses a user that does not carry it.
    owner: str
    uid: str
    home: str
    root: Remote
    login: Remote

    @classmethod
    def create(cls, wire: Wire, out_dir: Path, budget: Budget) -> NodeUser:
        root = Remote(f"root@{wire.host}", out_dir, budget)
        probe = root.run(["true"], tag="root-probe", want=30)
        if probe.rc != 0:
            pytest.fail(
                "the root hop is not set up: the nodes-e2e workflow authorizes "
                "the test key for root@mdssh before this tier runs\n" + probe.show()
            )
        pub = Path(f"{wire.key}.pub").read_text(encoding="utf-8").strip()
        name = f"mgn{secrets.token_hex(3)[:5]}"
        owner = f"{OWNER_PREFIX} {secrets.token_hex(8)}"
        try:
            made = root.script(_CREATE_USER, name, pub, owner, tag="useradd", want=60)
        except BaseException:
            # A timeout: the useradd may have finished on the far side of it.
            _discard(root, name, owner)
            raise
        if made.rc in (_BAD_NAME, _EXISTS):
            pytest.fail(
                f"the root hop refused to create {name}; nothing was created, "
                f"so nothing is deleted\n{made.show()}"
            )
        # From here a user carrying this run's stamp may exist: any way out
        # (a failure, a timeout, an answer that does not parse) deletes it.
        try:
            if made.rc != 0:
                pytest.fail(f"could not create the node user {name}\n{made.show()}")
            lines = made.out.strip().splitlines()
            answer = re.fullmatch(r"(\d+) (/.+)", lines[-1] if lines else "")
            if answer is None:
                pytest.fail(f"useradd's answer is not '<uid> <home>'\n{made.show()}")
            uid, home = answer.groups()
            user = cls(
                name=name,
                owner=owner,
                uid=uid,
                home=home,
                root=root,
                login=Remote(f"{name}@{wire.host}", out_dir, budget),
            )
            boot = user.login.script(_BOOTSTRAP_USER, wire.port, tag="bootstrap")
            if boot.rc != 0:
                pytest.fail(f"could not bootstrap the node user {name}\n{boot.show()}")
        except BaseException:
            _discard(root, name, owner)
            raise
        return user

    def delete(self) -> Run:
        return self.root.script(
            _DELETE_USER,
            self.name,
            self.owner,
            tag="userdel",
            timeout=CLEANUP_TIMEOUT_S,
        )


def _discard(root: Remote, name: str, owner: str) -> None:
    """Delete the user a failed ``create`` may have left, by its stamp. Its
    own failure goes to stderr, never up: the create's failure is the one
    the report is about, and it is re-raised as it was."""
    try:
        gone = root.script(
            _DELETE_USER, name, owner, tag="userdel", timeout=CLEANUP_TIMEOUT_S
        )
    except pytest.fail.Exception as exc:
        sys.stderr.write(f"cleanup of node user {name} failed: {exc}\n")
        return
    if gone.rc != 0:
        sys.stderr.write(f"cleanup of node user {name} failed:\n{gone.show()}\n")


# ---------------------------------------------------------------------------
# The PC side: repo, config, shim
# ---------------------------------------------------------------------------


def install_stand_in(dest: Path) -> None:
    """The stand-in and a byte copy of the product's encoder module, as the
    test repo commits them (``<repo>/.magent-e2e/``)."""
    from magent.sessions import claude

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(AGENT_SRC, dest / "node_agent.py")
    shutil.copyfile(Path(str(claude.__file__)), dest / "claude_encoding.py")


def git_env(wire: Wire, home: Path) -> dict[str, str]:
    """The harness's own git: an explicit ssh with the test key and BatchMode,
    no repo variables inherited from whatever launched pytest, and ``home``
    for HOME and XDG_CONFIG_HOME. The rig is module-scoped, so it runs
    OUTSIDE conftest's per-test HOME redirect: an inherited HOME here is the
    runner's real one, and git would read its real ``~/.gitconfig``."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_SSH_COMMAND"] = shlex.join(
        [
            "ssh",
            "-i",
            str(wire.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "LogLevel=ERROR",
        ]
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


_GIT_IDENT = (
    "-c",
    "user.name=magent-e2e",
    "-c",
    "user.email=e2e@magent.invalid",
    "-c",
    "init.defaultBranch=main",
    "-c",
    "core.autocrlf=false",
)


@dataclass
class Pc:
    """One PC: a tmp home, a config, a local clone."""

    home: Path
    cfg: Path
    repo: Path


def write_config(pc: Pc, *, host: str, user: str) -> None:
    """One project pinned to ``loop``. psmux and the upload server are off, and
    the tool command is the registry default (``claude --continue``), so the
    node runs exactly what a real config produces: the first start drops
    ``--continue`` (DECISION-11c), a restart keeps it. The version is the
    branch's own, so no load prints a version warning."""
    from magent import config

    pc.cfg.write_text(
        json.dumps(
            {
                "version": config.SCHEMA_VERSION,
                "settings": {
                    "defaultTool": "claude",
                    "psmux": False,
                    "uploadServer": False,
                    "nodes": {NICK: {"host": host, "user": user, "root": "~/magent"}},
                    # Short enough to bound the daemon legs, long enough that
                    # a snapshot stays fresh (2 intervals) across one CLI run.
                    "nodeSync": {"pullIntervalS": 5, "sampleIntervalS": 5},
                },
                "projects": [{"path": str(pc.repo), "tool": "claude", "node": NICK}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def write_psmux_shim(bin_dir: Path, log: Path) -> None:
    """A ``psmux`` that records every argv (tab-separated, one call a line)
    and exits 1 -- "no such session" to every probe. A node project must
    never reach it; where the product does call it, the log says for what."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "psmux"
    shim.write_text(
        "#!/bin/sh\n"
        f"{{ printf 'psmux'; for a in \"$@\"; do printf '\\t%s' \"$a\"; done; "
        f"printf '\\n'; }} >> {shlex.quote(str(log))}\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def read_shim(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [
        line.split("\t")[1:]
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


@dataclass
class NodeRig:
    """A module's world: one disposable node user, its ssh origin, PC-A (and
    any later PC) with a clone of one project pinned to ``loop``."""

    wire: Wire
    budget: Budget
    base: Path
    name: str
    user: NodeUser
    origin_url: str
    shim_dir: Path
    shim_log: Path
    pcs: list[Pc] = field(default_factory=list)
    spawned: list[subprocess.Popen[bytes]] = field(default_factory=list)
    # The stages later tests build on, by name ("D7", ...), once they passed.
    passed: set[str] = field(default_factory=set)

    @property
    def out(self) -> Path:
        return self.base / "out"

    @property
    def sid(self) -> str:
        from magent.psmux import session_name

        return session_name(self.name)

    @property
    def node(self) -> Remote:
        return self.user.login

    def product_node(self) -> Node:
        """The ``Node`` the product resolves this rig's config to."""
        from magent.nodes import Node

        return Node(
            nick=NICK, host=self.wire.host, user=self.user.name, root="~/magent"
        )

    @property
    def remote_dir(self) -> str:
        return f"{self.user.home}/magent/{self.name}"

    @property
    def node_projects_dir(self) -> str:
        """The node's ``~/.claude/projects/<enc(remote dir)>`` -- where the
        stand-in writes its transcript and bring_up.sh seeds memory."""
        from magent.nodes import encoded_project_dir

        return (
            f"{self.user.home}/.claude/projects/{encoded_project_dir(self.remote_dir)}"
        )

    # -- building ----------------------------------------------------------

    @classmethod
    def build(cls, wire: Wire, budget: Budget, base: Path, slice_: str) -> NodeRig:
        (base / "out").mkdir(parents=True, exist_ok=True)
        user = NodeUser.create(wire, base / "out", budget)
        name = f"mgn-{token(6)}-{slice_}"
        rig = cls(
            wire=wire,
            budget=budget,
            base=base,
            name=name,
            user=user,
            origin_url=(
                f"ssh://{user.name}@localhost:{wire.port}{user.home}/origin/{name}.git"
            ),
            shim_dir=base / "bin",
            shim_log=base / "psmux-calls.log",
        )
        try:
            rig.node.must(
                ["git", *_GIT_IDENT, "init", "-q", "--bare", rig.origin_path],
                tag="origin",
            )
            write_psmux_shim(rig.shim_dir, rig.shim_log)
            rig.pcs.append(rig._first_pc())
        except BaseException:
            rig.close()
            raise
        return rig

    @property
    def origin_path(self) -> str:
        return f"{self.user.home}/origin/{self.name}.git"

    def git(self, *args: str, cwd: Path, want: float = 60.0) -> str:
        run = run_files(
            ["git", *_GIT_IDENT, *args],
            self.out,
            "git",
            clamp(self.budget, want, f"git {args[0]}"),
            env=git_env(self.wire, self.base),
            cwd=cwd,
        )
        if run.rc != 0:
            pytest.fail(f"harness git failed:\n{run.show()}")
        return run.out.strip()

    def _new_pc_home(self, label: str) -> Path:
        home = self.base / f"home-{label}"
        home.mkdir()
        return home

    def _first_pc(self) -> Pc:
        """PC-A: a repo created here and pushed to the origin, the stand-in
        committed in it, ``.env`` and ``scratch.log`` beside it uncommitted,
        and a Claude memory for the project in the PC home."""
        repo = self.base / AWKWARD_DIR / self.name
        repo.mkdir(parents=True)
        self.git("init", "-q", cwd=repo)
        install_stand_in(repo / AGENT_DIR)
        (repo / ".gitignore").write_text(
            ".env\nscratch.log\n__pycache__/\n", encoding="utf-8"
        )
        (repo / "CLAUDE.md").write_text(f"# {self.name}\n", encoding="utf-8")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "init", cwd=repo)
        self.git("remote", "add", "origin", self.origin_url, cwd=repo)
        self.git("push", "-q", "-u", "origin", "main", cwd=repo)
        (repo / ".env").write_bytes(ENV_BYTES)
        (repo / "scratch.log").write_text("never ships\n", encoding="utf-8")
        pc = Pc(home=self._new_pc_home("a"), cfg=self.base / "a.config.json", repo=repo)
        memory = self.local_projects_dir(pc) / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text(self.memory_line + "\n", encoding="utf-8")
        write_config(pc, host=self.wire.host, user=self.user.name)
        return pc

    def second_pc(self) -> Pc:
        """PC-B: its own home and its own clone of the same origin, the same
        config lines (the origin URL comes from the clone)."""
        parent = self.base / f"{AWKWARD_DIR} b"
        parent.mkdir()
        repo = parent / self.name
        self.git(
            "clone", "-q", "--branch", "main", self.origin_url, str(repo), cwd=parent
        )
        (repo / ".env").write_bytes(ENV_BYTES)
        pc = Pc(home=self._new_pc_home("b"), cfg=self.base / "b.config.json", repo=repo)
        write_config(pc, host=self.wire.host, user=self.user.name)
        self.pcs.append(pc)
        return pc

    @property
    def memory_line(self) -> str:
        return f"- e2e memory {self.name}"

    def local_projects_dir(self, pc: Pc) -> Path:
        from magent.nodes import encoded_project_dir

        # The product encodes the RESOLVED project folder.
        real = os.path.realpath(pc.repo)
        return pc.home / ".claude" / "projects" / encoded_project_dir(real)

    # -- the PC side -------------------------------------------------------

    def env(self, pc: Pc, *, sync: bool = False) -> dict[str, str]:
        return child_env(pc.home, sync=sync, extra_path=[self.shim_dir])

    def magent(
        self,
        *args: str,
        tag: str,
        pc: Pc | None = None,
        sync: bool = False,
        want: float = 120.0,
    ) -> Run:
        pc = pc or self.pcs[0]
        return run_files(
            [sys.executable, "-m", "magent", "--config", str(pc.cfg), *args],
            self.out,
            f"magent-{tag}",
            clamp(self.budget, want, f"magent-{tag}"),
            env=self.env(pc, sync=sync),
            cwd=self.base,
        )

    def spawn(self, argv: Sequence[str], *, pc: Pc, sync: bool, tag: str) -> int:
        """A long-lived PC-side child (serve), killed by pid in ``close``."""
        out = (self.out / f"{tag}.out").open("wb")
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=self.env(pc, sync=sync),
            cwd=self.base,
        )
        out.close()
        self.spawned.append(proc)
        return proc.pid

    def node_map(self, pc: Pc | None = None) -> dict[str, dict[str, object]]:
        path = (pc or self.pcs[0]).home / ".magent" / "nodes" / "node-map.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def mirror_dir(self, pc: Pc | None = None) -> Path:
        return (
            (pc or self.pcs[0]).home
            / ".magent"
            / "nodes"
            / NICK
            / self.sid
            / "transcripts"
        )

    def marks(self, pc: Pc | None = None) -> dict[str, dict[str, object]]:
        path = (pc or self.pcs[0]).home / ".magent" / "nodes" / NICK / "pull.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def status(self, *, tag: str, sync: bool = False) -> dict[str, object]:
        run = self.magent("status", "--json", tag=tag, sync=sync, want=60)
        try:
            return json.loads(run.out)
        except ValueError:
            pytest.fail(f"status --json did not print JSON\n{run.show()}")

    def node_row(self, status: dict[str, object]) -> dict[str, object]:
        rows = status.get("node_sessions")
        assert isinstance(rows, list), status
        mine = [r for r in rows if isinstance(r, dict) and r.get("name") == self.name]
        assert len(mine) == 1, rows
        return mine[0]

    def daemon_pid(self, pc: Pc | None = None) -> int | None:
        path = (pc or self.pcs[0]).home / ".magent" / "node-sync.pid"
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def live_daemon(self, pc: Pc | None = None) -> int | None:
        """The pid of this PC's running ``node sync`` daemon, or None. The
        pid file alone does not say: it outlives its process, and the pid
        may have been recycled."""
        pc = pc or self.pcs[0]
        pid = self.daemon_pid(pc)
        if pid is None or not alive(pid, self.budget):
            return None
        return pid if is_sync_daemon(_cmdline(pid), pc.cfg) else None

    def shim_calls(self) -> list[list[str]]:
        return read_shim(self.shim_log)

    def reset_shim(self) -> None:
        self.shim_log.unlink(missing_ok=True)

    # -- the node side -----------------------------------------------------

    def tmux(self, *args: str, tag: str) -> Run:
        from magent.attach_client import TMUX_SOCKET

        return self.node.run(["tmux", "-L", TMUX_SOCKET, *args], tag=f"tmux-{tag}")

    def session_rc(self) -> int:
        """``has-session -t =<sid>``'s exit code on the node: 0 up, 1 not."""
        return self.tmux("has-session", "-t", f"={self.sid}", tag="has").rc

    def agent_log(self) -> list[dict[str, object]]:
        raw = self.node.read_bytes(
            f"{self.user.home}/.magent-e2e/agent-log.jsonl", tag="agent-log"
        )
        if raw is None:
            return []
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]

    def starts(self) -> list[dict[str, object]]:
        return [r for r in self.agent_log() if r.get("event") == "start"]

    def node_transcripts(self) -> dict[str, bytes]:
        """Every file under the node's project transcript dir, by name."""
        listed = self.node.run(
            ["find", self.node_projects_dir, "-maxdepth", "1", "-type", "f"],
            tag="find-transcripts",
        )
        out: dict[str, bytes] = {}
        for path in listed.out.splitlines() if listed.rc == 0 else []:
            data = self.node.read_bytes(path, tag="transcript")
            if data is not None:
                out[path.rsplit("/", 1)[-1]] = data
        return out

    def in_transcript(self, text: str) -> bool:
        """Whether any node transcript of this project holds ``text``."""
        return any(text.encode() in data for data in self.node_transcripts().values())

    def capture(self, *, tag: str) -> str:
        """The node pane's text, its scrollback included. ``=sid:`` because
        capture-pane takes a PANE target."""
        run = self.tmux(
            "capture-pane", "-p", "-J", "-S", "-", "-t", f"={self.sid}:", tag=tag
        )
        if run.rc != 0:
            pytest.fail(f"capture-pane of {self.sid} failed\n{run.show()}")
        return run.out

    def clients(self) -> set[str]:
        """The ttys of the clients attached to the node session; empty when
        the read fails (a wait on it then fails, naming what it waited for)."""
        run = self.tmux(
            "list-clients", "-F", "#{client_tty}", "-t", f"={self.sid}", tag="clients"
        )
        return set(run.out.split()) if run.rc == 0 else set()

    def poke(self, tok: str) -> None:
        """Type ``poke <tok>`` into the node pane the way the fleet wire does
        (literal text, then a separate Enter), and wait for the stand-in to
        have written it to its transcript."""
        target = f"={self.sid}:"
        for args in (("-l", f"poke {tok}"), ("Enter",)):
            run = self.tmux("send-keys", "-t", target, *args, tag="send-keys")
            if run.rc != 0:
                pytest.fail(f"send-keys into {self.sid} failed\n{run.show()}")
        wait_for(
            f"the node transcript records 'poke {tok}'",
            lambda: self.in_transcript(f"poke {tok}"),
            30,
            budget=self.budget,
            explain=self.diag,
        )

    def drop_pty_connections(self) -> Run:
        """Kill the NODE side of every pty ssh connection the node user holds:
        a dropped network as the PC's ssh sees it -- the server end goes away,
        ssh exits 255, the attach client redials. Killing the LOCAL ssh would
        not be a drop at all: the client probes, finds the session alive and
        reads it as a detach, by design (``attach_client.verdict``).

        Only pty connections are titled ``@pts/``; the product's own remote
        calls, this one included, run without a pty (``@notty``) and survive.
        The ``^`` keeps this command's own bash, whose argv carries the
        pattern, from matching itself. rc 0: something was killed."""
        return self.node.run(
            [
                "pkill",
                "-KILL",
                "-u",
                self.user.name,
                "-f",
                "^sshd(-session)?: [^ ]+@pts/",
            ],
            tag="drop-pty",
        )

    # -- diagnostics and teardown ------------------------------------------

    def diag(self) -> str:
        """What a failed stage needs to be read, since a CI-only failure has
        no local repro: on the node, its sessions, the pane, the stand-in's
        log, the user's processes, the state store and the transcripts; on
        each PC, node-map.json, the pull marks, the mirror (sizes and
        mtimes) and the log tails; and the shim's calls. Never raises.

        The node reads share one allowance (``DIAG_S``, or ``DIAG_FLOOR_S``
        once the module budget is spent), so a wedged sshd costs a failure at
        most that long; a read with nothing left is skipped, named, and the
        reads run most-telling first.

        No redaction pass: nothing secret reaches these files today (the
        canaries and ``.env`` are fixed test bytes, and the node user's key is
        never read). A tier that gives a child a real credential needs one."""
        from magent.attach_client import TMUX_SOCKET

        parts: list[str] = []
        allowance = Budget(max(DIAG_FLOOR_S, min(DIAG_S, self.budget.remaining())))

        def node(title: str, argv: Sequence[str]) -> None:
            timeout = allowance.clamp(DIAG_READ_S)
            if timeout < DIAG_MIN_READ_S:
                parts.append(f"--- node: {title} ---\n(skipped: diag allowance spent)")
                return
            try:
                remote = "bash -c " + shlex.quote(shlex.join(argv))
                done = subprocess.run(
                    ["ssh", *SSH_OPTS, self.node.target, remote],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
                text = (done.stdout + done.stderr).decode("utf-8", "replace")
            except (OSError, subprocess.TimeoutExpired) as exc:
                text = f"(unavailable: {exc})"
            parts.append(f"--- node: {title} ---\n{text}")

        home = self.user.home
        full_iso = "--time-style=full-iso"
        node("tmux sessions", ["tmux", "-L", TMUX_SOCKET, "list-sessions"])
        node(
            "pane",
            [
                *("tmux", "-L", TMUX_SOCKET, "capture-pane", "-p", "-J"),
                *("-S", f"-{DIAG_TAIL}", "-t", f"={self.sid}:"),
            ],
        )
        node(
            "agent log",
            ["tail", "-n", str(DIAG_TAIL), f"{home}/.magent-e2e/agent-log.jsonl"],
        )
        node("processes", ["ps", "-o", "pid,etime,args", "-u", self.user.name])
        node("state store", ["ls", "-la", full_iso, f"{home}/.magent/state"])
        node("transcripts", ["ls", "-la", full_iso, self.node_projects_dir])
        node("magent dir", ["ls", "-la", f"{home}/magent"])
        # The node reaches its own origin over ssh (the bring-up's clone, every
        # fetch). When that fails the product says only "git clone … failed";
        # git's own words and ssh -v say which half of the wire refused.
        node(
            "git ls-remote origin",
            [
                "sh",
                "-c",
                f"git ls-remote {shlex.quote(self.origin_url)} 2>&1 | tail -n 10",
            ],
        )
        node(
            "ssh -v to its own origin",
            [
                "sh",
                "-c",
                (
                    "ssh -v -o BatchMode=yes -o ConnectTimeout=5 "
                    f"-p {shlex.quote(self.wire.port)} localhost true 2>&1 | tail -n 25"
                ),
            ],
        )
        for i, pc in enumerate(self.pcs):
            nodes_dir = pc.home / ".magent" / "nodes"
            parts.append(
                f"--- pc{i}: node-map.json ---\n"
                + _text_or_why(nodes_dir / "node-map.json")
            )
            parts.append(
                f"--- pc{i}: pull marks ---\n"
                + _text_or_why(nodes_dir / NICK / "pull.json")
            )
            parts.append(f"--- pc{i}: mirror ---\n{_listing(self.mirror_dir(pc))}")
            for log in sorted((pc.home / ".magent" / "logs").glob("*.log")):
                lines = _text_or_why(log).splitlines()
                parts.append(
                    f"--- pc{i}: {log.name} (tail) ---\n"
                    + "\n".join(lines[-DIAG_TAIL:])
                )
        parts.append(f"--- psmux shim calls ---\n{self.shim_calls()}")
        return "\n".join(parts)

    def close(self) -> list[str]:
        """Kill every PC-side child this rig started (by its Popen), stop any
        sync daemon a PC home holds, then delete the node user -- which takes
        its tmux server, its stand-ins and its sessions with it. Returns what
        could not be cleaned, a timed-out delete included, for the fixture to
        fail on; never raises past a step, so every step runs."""
        problems: list[str] = []
        # The PC side shares ONE allowance, so teardown has a fixed bound; the
        # user delete keeps its own, so a wedged PC-side stop can never cost
        # the node its cleanup. Each step gets at least a second.
        pc_side = Budget(CLEANUP_TIMEOUT_S)
        for proc in self.spawned:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=max(1.0, pc_side.clamp(10)))
                except subprocess.TimeoutExpired:
                    problems.append(f"pid {proc.pid} did not die")
        for pc in self.pcs:
            try:
                subprocess.run(
                    [
                        sys.executable,
                        *("-m", "magent", "--config", str(pc.cfg)),
                        *("node", "sync", "--stop"),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=max(1.0, pc_side.clamp(30)),
                    env=self.env(pc),
                    cwd=self.base,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                problems.append(f"node sync --stop timed out for {pc.home}")
            pid = self.daemon_pid(pc)
            if pid is None or not _alive(pid):
                continue
            # A pid file outlives its process, and the pid may be recycled:
            # only this PC's own `node sync` is ever killed (the product's
            # stop_daemon does not trust the file either).
            if is_sync_daemon(_cmdline(pid), pc.cfg):
                problems.append(f"sync daemon pid {pid} survived --stop (killed)")
                _kill(pid)
            else:
                problems.append(
                    f"{pc.home}'s node-sync.pid names live pid {pid}, which is "
                    "not this PC's node sync: left alone"
                )
        try:
            deleted = self.user.delete()
        except pytest.fail.Exception as exc:
            problems.append(f"node user {self.user.name} not deleted: {exc}")
        else:
            if deleted.rc != 0:
                problems.append(
                    f"node user {self.user.name} not deleted:\n{deleted.show()}"
                )
        return problems


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except OSError:
        return


def _cmdline(pid: int) -> bytes:
    """``/proc/<pid>/cmdline`` (the tier is Linux-only), or b"" when there is
    none to read -- which matches nothing."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def is_sync_daemon(cmdline: bytes, cfg: Path) -> bool:
    """Whether a NUL-separated ``cmdline`` is the ``node sync`` daemon of the
    PC whose config is ``cfg``: ``<python> -m magent --config <cfg> node
    sync`` (``launch.node_sync_argv``), the config as given or resolved."""
    argv = [a.decode("utf-8", "replace") for a in cmdline.split(b"\0") if a]
    if argv[1:3] != ["-m", "magent"] or argv[-2:] != ["node", "sync"]:
        return False
    configs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--config"]
    return configs in ([str(cfg)], [os.path.realpath(cfg)])


def _text_or_why(path: Path) -> str:
    """A PC-side file for diag(): its text, or why there is none."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "(absent)"
    except OSError as exc:
        return f"(unreadable: {exc})"


def _listing(folder: Path) -> str:
    """Every file under ``folder``: size, mtime (UTC, ms) and relative path."""
    if not folder.is_dir():
        return "(absent)"
    rows: list[str] = []
    for path in sorted(folder.rglob("*")):
        try:
            st = path.stat()
        except OSError as exc:
            rows.append(f"(unreadable: {exc})")
            continue
        if not path.is_file():
            continue
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(st.st_mtime))
        millis = st.st_mtime_ns // 1_000_000 % 1000
        rel = path.relative_to(folder).as_posix()
        rows.append(f"{st.st_size:>10}  {stamp}.{millis:03d}Z  {rel}")
    return "\n".join(rows) or "(empty)"


def alive(pid: int, budget: Budget) -> bool:
    """Whether ``pid`` is a live process on this machine (a zombie counts:
    the daemon is detached, so no one here will reap it -- ``ps`` tells)."""
    if not _alive(pid):
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=clamp(budget, 10, f"ps {pid}"),
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def lines_with(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


# What psmux.stop_sessions issues: a liveness probe and a kill, nothing else.
STOP_VERBS = frozenset({"has-session", "kill-server"})


def only_stops(calls: Iterable[list[str]], sid: str) -> bool:
    """Every recorded psmux call is ``-L <sid>`` and then a probe or a kill:
    ``down`` may stop the local half of a node name, never type into it or
    create it."""
    return all(
        call[:2] == ["-L", sid] and len(call) > 2 and call[2] in STOP_VERBS
        for call in calls
    )
