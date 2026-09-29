#!/usr/bin/env bash
# magent node provision: lays this PC's Claude Code user scope onto the node.
# Fed over stdin by remote_mux.provision (`bash -s -- <socket> [--force]`;
# lib.sh takes the socket, which this script does not use). After the
# sentinel comes the gh token line (empty when there is none), then a gzip tar
# holding the scope AND node_apply.py, which does the work. This shim only
# unpacks it into a private temp dir and hands the token over a pipe --
# never an argument, never a file.
set -euo pipefail
# The token is expanded below: a trace switched on through SHELLOPTS or a
# BASH_ENV file would print it to stderr. Off before anything else runs.
set +o xtrace
# @include lib.sh

WORK=""

cleanup() {
  if [ -n "$WORK" ]; then rm -rf -- "$WORK"; fi
}

main() {
  local force="" token="" rc=0
  if [ "$#" -gt 1 ]; then
    printf 'fail\tprovision\texpected at most one argument (--force), got %s\n' "$#"
    return 2
  fi
  case "${1:-}" in
    "") ;;
    --force) force=--force ;;
    *) printf 'fail\tprovision\tunknown argument: %s\n' "$1"; return 2 ;;
  esac
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'fail\tpython3\tpython3 is not installed on this node -- run: magent node setup\n'
    return 1
  fi
  # node_apply.py runs on 3.8 and nothing older. </dev/null: the payload is
  # still on stdin.
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    </dev/null >/dev/null 2>&1; then
    printf 'fail\tpython3\tpython3 on this node is older than 3.8 -- run: magent node setup\n'
    return 1
  fi
  umask 077
  # The trap before the dir: a signal between the two would strand it.
  trap cleanup EXIT
  WORK=$(mktemp -d)
  if ! { IFS= read -r token || true; tar -xzf - -C "$WORK"; } < <(magent_payload); then
    printf 'fail\tpayload\tthe provisioning payload did not unpack\n'
    return 1
  fi
  token=${token%$'\r'}
  exec </dev/null
  # The native Claude installer puts claude in ~/.local/bin, which the PATH
  # of a non-login ssh command may lack.
  export PATH="$HOME/.local/bin:$PATH"
  # printf is a builtin: the token is never any process's argv. sys.path[0]
  # is REPLACED, not prepended to: under -c it is the cwd -- $HOME, over ssh --
  # and a ~/json.py there would shadow the stdlib. The --opt=value forms keep
  # a value that starts with "-" (TMPDIR=-t) from reading as an option.
  printf '%s\n' "$token" | PYTHONIOENCODING=utf-8 python3 -B -s -c \
    'import sys; sys.path[0] = sys.argv[1]; from node_apply import main; sys.exit(main(sys.argv[2:]))' \
    "$WORK" --work="$WORK" --path="$PATH" ${force:+"$force"} || rc=$?
  return "$rc"
}

main "$@"; exit $?
