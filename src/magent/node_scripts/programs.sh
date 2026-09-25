#!/usr/bin/env bash
# magent node programs: which of the named programs this node resolves.
# Fed by remote_mux.node_programs (`bash -s -- <socket> <program>...`; lib.sh
# takes the socket) before a provision that carries stdio MCP candidates. One
# row per program: ok<TAB>program<TAB>path, or skip<TAB>program<TAB>not found.
# Reads and writes nothing. PATH is provision.sh's: ~/.local/bin first.
set -euo pipefail
# @include lib.sh

main() {
  local program found
  export PATH="$HOME/.local/bin:$PATH"
  for program in "$@"; do
    if found=$(command -v -- "$program" 2>/dev/null); then
      printf 'ok\t%s\t%s\n' "$program" "$found"
    else
      printf 'skip\t%s\tnot found\n' "$program"
    fi
  done
}

main "$@"; exit $?
