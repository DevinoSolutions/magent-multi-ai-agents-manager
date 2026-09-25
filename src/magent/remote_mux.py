"""The single owner of every subprocess aimed at a node (spec §6).

ssh argv, ``tmux -L magent`` on the node, the shipped ``node_scripts``, and the
local git reads a bring-up needs. Every function returns data or raises
``RemoteError``; nothing here prints or exits. Four laws:

- a remote command is a list, sent as ``bash -c <shlex.quote(shlex.join(argv))>``
  -- never an f-string shell, and never parsed by the node user's LOGIN shell
  (zsh expands a bare ``=word``, which is every exact tmux target ``=<sid>``);
- every call is bounded: ``timeout_s`` is a required keyword, so a call that
  forgot it is a TypeError, never a hang;
- secrets travel on stdin only -- never argv, never a log line;
  ``RemoteError.command_redacted`` names stdin by its length alone;
- ``BatchMode=yes`` everywhere: a password prompt nobody can answer is a hang.
"""

from __future__ import annotations

import functools
import shlex
import shutil
from typing import TYPE_CHECKING

from magent.attach_client import SSH_CONNECTION_OPTS, TMUX_SOCKET

if TYPE_CHECKING:
    from magent.nodes import Node

# tmux, not psmux: nodes are Linux. One server per node user (`-L magent`,
# D10). The name has one owner, attach_client, whose pane attaches to it; this
# is a re-export, never a second literal (DECISION-3).
MUX = "tmux"
SOCKET = TMUX_SOCKET

# The default bounds (spec §6): a probe is one round trip, a script is one
# connection doing real work, a bring-up clones repositories.
PROBE_TIMEOUT_S = 10.0
SCRIPT_TIMEOUT_S = 120.0
BRING_UP_TIMEOUT_S = 600.0


class RemoteError(RuntimeError):
    """A node call that failed: ``rc`` (None when it never finished -- a spawn
    failure or a timeout), the last lines of its stderr, and the argv it ran
    with stdin reduced to its length. Never file contents, never a token."""

    def __init__(
        self, rc: int | None, stderr_tail: str, command_redacted: tuple[str, ...]
    ) -> None:
        self.rc = rc
        self.stderr_tail = stderr_tail
        self.command_redacted = command_redacted
        super().__init__(
            f"{shlex.join(command_redacted)} failed (rc={rc}): {stderr_tail}"
        )


@functools.lru_cache(maxsize=1)
def find_ssh() -> str | None:
    """The ssh client on PATH, or None. Cached for the process lifetime like
    ``psmux.find_psmux``: a test that changes PATH clears it on the way in and
    out. Tests never see the real one (tests/conftest.py::_no_real_ssh)."""
    return shutil.which("ssh")


def ssh_argv(
    node: Node, remote_cmd: str, *, tty: bool = False, batch: bool = True
) -> list[str]:
    """``ssh`` argv for one command on ``node``. The option list is
    ``attach_client``'s -- the one owner of how magent dials a host."""
    argv = ["ssh", *SSH_CONNECTION_OPTS]
    if batch:
        argv += ["-o", "BatchMode=yes"]
    if tty:
        argv.append("-t")
    return [*argv, node.target, remote_cmd]
