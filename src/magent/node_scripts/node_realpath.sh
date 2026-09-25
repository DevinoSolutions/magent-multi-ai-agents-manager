#!/usr/bin/env bash
# node_realpath.sh <path> -- print the physical absolute path <path> names on
# this node: "~" expanded, symlinks resolved, and a not-yet-existing tail kept
# (a recall --to target is installed before its clone). Claude Code keys its
# project dir by this path; magent encodes it -- the node never does.
# python3 rather than `realpath -m`, which BSD/macOS realpath lacks.
# The tmux socket arrives as $1 and lib.sh shifts it off (DECISION-26 ii).
set -euo pipefail
# @include lib.sh

main() {
  python3 -c 'import os, sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' \
    "${1:?usage: node_realpath.sh <path>}"
}

main "$@"; exit $?
