#!/usr/bin/env bash
# magent node provision: lays this PC's Claude Code user scope onto the node.
# Fed over stdin by remote_mux.provision (`bash -s -- <socket> [--force]`;
# lib.sh takes the socket, which this script does not use). After the
# sentinel comes the gh token line (empty when there is none), then a gzip tar
# holding the scope AND node_apply.py, which does the work. This shim only
# unpacks it into a private temp dir and hands the token over a pipe --
# never an argument, never a file.
set -euo pipefail
# @include lib.sh

WORK=""

cleanup() {
  if [ -n "$WORK" ]; then rm -rf -- "$WORK"; fi
}

main() {
  local force="" token="" rc=0
  case "${1:-}" in
    "") ;;
    --force) force=--force ;;
    *) printf 'fail\tprovision\tunknown argument: %s\n' "$1"; return 2 ;;
  esac
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'fail\tpython3\tpython3 is not installed on this node -- run: magent node setup\n'
    return 1
  fi
  umask 077
  WORK=$(mktemp -d)
  trap cleanup EXIT
  if ! { IFS= read -r token || true; tar -xzf - -C "$WORK"; } < <(magent_payload); then
    printf 'fail\tpayload\tthe provisioning payload did not unpack\n'
    return 1
  fi
  token=${token%$'\r'}
  exec </dev/null
  # The native Claude installer puts claude in ~/.local/bin, which the PATH
  # of a non-login ssh command may lack.
  export PATH="$HOME/.local/bin:$PATH"
  # printf is a builtin: the token is never any process's argv.
  printf '%s\n' "$token" | PYTHONIOENCODING=utf-8 python3 -B -s -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from node_apply import main; sys.exit(main(sys.argv[2:]))' \
    "$WORK" --work "$WORK" --path "$PATH" ${force:+"$force"} || rc=$?
  return "$rc"
}

main "$@"; exit $?
