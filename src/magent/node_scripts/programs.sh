#!/usr/bin/env bash
# magent node programs: which of the named programs this node resolves.
# Fed by remote_mux.node_programs (`bash -s -- <socket> <program>...`; lib.sh
# takes the socket) before a provision that carries stdio MCP candidates. One
# row per program: ok<TAB>program<TAB>path, or skip<TAB>program<TAB>why.
# Reads and writes nothing. PATH is provision.sh's: ~/.local/bin first.
# Only an executable FILE counts (`type -P`): a builtin, a keyword or a
# function -- cd, if, [[, this script's own main -- is nothing an MCP server
# can exec. A relative path depends on a cwd the server will not share.
set -euo pipefail
set +o xtrace
# @include lib.sh

main() {
  local program found
  export PATH="$HOME/.local/bin:$PATH"
  for program in "$@"; do
    case "$program" in
      /*) ;;
      */*)
        printf 'skip\t%s\ta relative path names no program\n' "$program"
        continue
        ;;
    esac
    if found=$(type -P -- "$program" 2>/dev/null) && [ -n "$found" ]; then
      printf 'ok\t%s\t%s\n' "$program" "$found"
    else
      printf 'skip\t%s\tnot found\n' "$program"
    fi
  done
}

main "$@"; exit $?
