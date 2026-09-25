#!/usr/bin/env bash
# magent node doctor: read-only health rows for this node user.
# Fed over stdin by remote_mux.doctor
# (`bash -s -- <socket> --root R --target T`); reads no payload. lib.sh takes
# the socket (required, no default) into MAGENT_SOCKET.
# Always exits 0: a finding is a row, and magent node doctor owns the exit code.
# No -e: every probe's failure is a finding, not a reason to stop.
set -uo pipefail
# @include lib.sh
# @include tmux_floor.sh

GIB_KB=1048576  # df -Pk reports KiB

say() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }

# check_tool <name> <status when missing> <version argv...>
check_tool() {
  local name=$1 missing=$2
  shift 2
  if command -v "$name" >/dev/null 2>&1; then
    say ok "$name" "$("$@" 2>&1 | head -n1)"
  else
    say "$missing" "$name" "$name is not on PATH -- run: magent node setup"
  fi
}

# tmux is on PATH AND at bring_up.sh's floor (DECISION-22): an old tmux is a
# fail, because the first bring-up would refuse it with rc 4.
check_tmux() {
  local verdict version floor
  verdict=$(magent_tmux_verdict)
  version=${verdict#*$'\t'}
  floor=$(magent_tmux_floor)
  case ${verdict%%$'\t'*} in
    ok) say ok tmux "$version" ;;
    missing) say fail tmux "tmux is not on PATH -- run: magent node setup" ;;
    unread) say fail tmux "cannot read the tmux version ($version); magent needs tmux $floor or newer" ;;
    *) say fail tmux "$version is too old; magent needs tmux $floor or newer -- upgrade tmux on this node" ;;
  esac
}

check_claude_login() {
  local target=$1 out
  if ! command -v claude >/dev/null 2>&1; then
    say skip claude-login "claude is not installed"
    return
  fi
  out=$(claude auth status --json 2>/dev/null) || true
  if [[ $out =~ \"loggedIn\"[[:space:]]*:[[:space:]]*true ]]; then
    say ok claude-login "logged in"
  else
    say fail claude-login "Claude Code is not logged in here -- run once: ssh $target claude"
  fi
}

check_github_key() {
  local out
  out=$(ssh -T -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=accept-new git@github.com 2>&1) || true
  if [[ $out =~ Hi\ ([^!]+)! ]]; then
    say ok github-key "authenticates as ${BASH_REMATCH[1]}"
  else
    say fail github-key "GitHub refused this node's key (${out##*$'\n'}) -- run: magent node setup"
  fi
}

check_locale() {
  local charmap
  charmap=$(locale charmap 2>/dev/null) || charmap=""
  if [ "$charmap" = UTF-8 ]; then
    say ok locale "UTF-8"
  else
    say warn locale "charmap is ${charmap:-unknown}, not UTF-8 -- set LANG=C.UTF-8 for this user"
  fi
}

check_disk() {
  local root=$1 dir avail
  dir=${root/#\~/$HOME}
  while [ ! -d "$dir" ] && [ "$dir" != / ] && [ "$dir" != . ]; do
    dir=$(dirname "$dir")
  done
  avail=$(df -Pk "$dir" 2>/dev/null | awk 'NR == 2 {print $4}') || avail=""
  if ! [[ $avail =~ ^[0-9]+$ ]]; then
    say warn disk "could not read the free space under $root"
  elif [ "$avail" -lt "$GIB_KB" ]; then
    say fail disk "$((avail / 1024)) MB free under $root"
  elif [ "$avail" -lt $((5 * GIB_KB)) ]; then
    say warn disk "$((avail / GIB_KB)) GB free under $root"
  else
    say ok disk "$((avail / GIB_KB)) GB free under $root"
  fi
}

check_sessions() {
  local n
  n=$(tmux -L "$MAGENT_SOCKET" list-sessions 2>/dev/null | wc -l) || n=0
  say ok sessions "$((n)) on tmux socket $MAGENT_SOCKET"
}

main() {
  local root=$HOME target="this node"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --root | --target)
        if [ "$#" -lt 2 ]; then
          say fail doctor "$1 needs a value"
          return 0
        fi
        case "$1" in
          --root) root=$2 ;;
          --target) target=$2 ;;
        esac
        shift 2
        ;;
      *)
        say fail doctor "unknown argument: $1"
        return 0
        ;;
    esac
  done
  exec </dev/null
  export PATH="$HOME/.local/bin:$PATH"
  check_tmux
  check_tool git fail git --version
  check_tool claude fail claude --version
  check_tool python3 fail python3 --version
  check_tool gh warn gh --version
  check_claude_login "$target"
  check_github_key
  check_locale
  check_disk "$root"
  check_sessions
  return 0
}

main "$@"; exit $?
