"""The shell scripts magent runs ON a node, shipped as package data.

Nothing is installed on the node to run them: ``remote_mux.run_script`` feeds
``<name>.sh`` to ``bash -s -- <socket> <args>`` over ssh's stdin, and a
payload (JSON, a tarball) may follow a sentinel line. Every script is POSIX
bash with a ``set -u -o pipefail`` line (``-e`` too, except doctor.sh, which
reports every failed probe as a row and always exits 0) and ends with
``main "$@"; exit $?`` -- bash reads a script from a pipe byte by byte, so
that last line hands whatever follows on stdin to ``main`` instead of
executing it.

Shared functions live in ``lib.sh`` and are INLINED at load time: a line that
is exactly ``# @include <file>.sh`` is replaced by that file's text. One
level only -- an included file may not include -- so a script's shape is
readable from two files at most.

The calling convention (DECISION-26 ii): ``$1`` is ALWAYS the tmux socket
name, ``remote_mux.SOCKET``, passed by ``run_script`` on every call. Every
script includes ``lib.sh`` right after the script's ``set`` line; its top level
reads ``$1`` into ``MAGENT_SOCKET`` (no default) and shifts it off, so
``main "$@"`` sees only the caller's own arguments. No script names the
socket; every tmux call is ``tmux -L "$MAGENT_SOCKET"``.
"""

from __future__ import annotations

from importlib import resources

_INCLUDE = "# @include "

# Packaged scripts that are NOT run_script entry points: they never receive the
# socket as $1 and never include lib.sh, and run_script refuses them. Every
# file -- these included -- must still never name the tmux socket. One reason
# per name; E adds state_hook.sh and F adds tmux_floor.sh in their PRs.
NON_ENTRY_SCRIPTS: frozenset[str] = frozenset(
    {
        # The shared library itself: inlined by `# @include`, never run alone.
        "lib.sh",
    }
)


def _read(name: str) -> str:
    """The packaged text of ``<name>.sh``. ``read_text``'s newline
    translation IS the LF guarantee for ``script()`` output -- a CRLF checkout
    still ships LF on the wire -- so never "optimize" it to
    ``read_bytes().decode()``."""
    return (
        resources.files("magent.node_scripts")
        .joinpath(f"{name}.sh")
        .read_text(encoding="utf-8")
    )


def script(name: str) -> str:
    """The text of ``node_scripts/<name>.sh`` with every ``# @include`` line
    expanded. FileNotFoundError for an unknown name (or include); ValueError
    for a nested include, and for a file included twice (lib.sh's top level
    shifts ``$1``: a second copy would shift again, and ``MAGENT_SOCKET``
    would silently become the caller's first argument). An include line is
    recognized only at column 0 as the whole line, and it is expanded even
    inside a heredoc -- so never write one there."""
    out: list[str] = []
    seen: set[str] = set()
    for line in _read(name).splitlines(keepends=True):
        if not line.startswith(_INCLUDE):
            out.append(line)
            continue
        target = line[len(_INCLUDE) :].strip().removesuffix(".sh")
        if target in seen:
            raise ValueError(f"{name}.sh: {target}.sh is included more than once")
        seen.add(target)
        included = _read(target)
        if any(inner.startswith(_INCLUDE) for inner in included.splitlines()):
            raise ValueError(f"{name}.sh: nested # @include in {line.strip()!r}")
        out.append(included if included.endswith("\n") else included + "\n")
    return "".join(out)
