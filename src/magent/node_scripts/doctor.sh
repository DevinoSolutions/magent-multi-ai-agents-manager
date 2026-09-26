#!/usr/bin/env bash
# magent node doctor: health rows for this node user, read-only apart from
# known_hosts TOFU (check_github_key).
# Fed over stdin by remote_mux.doctor
# (`bash -s -- <socket> --root R --target T`); reads no payload. lib.sh takes
# the socket (required, no default) into MAGENT_SOCKET.
# Always exits 0: a finding is a row, and magent node doctor owns the exit code.
# No -e: every probe's failure is a finding, not a reason to stop.
set -uo pipefail
# @include lib.sh
# @include tmux_floor.sh

GIB_KB=1048576  # df -Pk reports KiB

# A probe that can stall -- a token refresh, a DNS lookup, a wedged tmux
# server, a hung mount -- runs under its OWN time limit, so one stuck probe
# is one row, never the whole report lost to remote_mux.DOCTOR_TIMEOUT_S
# (which covers every one of them hanging at once; pinned by test).
CLAUDE_PROBE_S=8
GITHUB_PROBE_S=12
TMUX_PROBE_S=4
DF_PROBE_S=4
VERSION_PROBE_S=4  # each `<tool> --version`, and `tmux -V`
PROBE_KILL_S=2  # a probe that ignores TERM gets KILL this much later

say() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }

# bounded <seconds> <argv...>: argv under its own time limit.
bounded() {
  local seconds=$1
  shift
  timeout -k "$PROBE_KILL_S" "$seconds" "$@"
}

# timed_out <rc>: did a bounded probe run out of time (TERM 124, KILL 137)?
timed_out() { [ "$1" -eq 124 ] || [ "$1" -eq 137 ]; }

# check_tool <name> <status when missing> <version argv...>
# A version read that hangs gets the same status as a missing tool; one that
# exits non-zero warns, since what it printed is not a working tool's version.
check_tool() {
  local name=$1 missing=$2 out rc
  shift 2
  if ! command -v "$name" >/dev/null 2>&1; then
    say "$missing" "$name" "$name is not on PATH -- run: magent node setup"
    return
  fi
  out=$(bounded "$VERSION_PROBE_S" "$@" 2>&1)
  rc=$?
  if timed_out "$rc"; then
    say "$missing" "$name" "$* timed out after ${VERSION_PROBE_S}s"
  elif [ "$rc" -ne 0 ]; then
    say warn "$name" "$* exited $rc -- reinstall $name on this node"
  else
    say ok "$name" "${out%%$'\n'*}"
  fi
}

# tmux is on PATH AND at bring_up.sh's floor (DECISION-22): an old tmux is a
# fail, because the first bring-up would refuse it with rc 4.
check_tmux() {
  local out rc verdict version floor
  if ! command -v tmux >/dev/null 2>&1; then
    say fail tmux "tmux is not on PATH -- run: magent node setup"
    return
  fi
  out=$(bounded "$VERSION_PROBE_S" tmux -V 2>/dev/null)
  rc=$?
  if timed_out "$rc"; then
    say fail tmux "tmux -V timed out after ${VERSION_PROBE_S}s"
    return
  fi
  # A version printed by a failing tmux -V is not graded: the binary is broken.
  if [ "$rc" -ne 0 ]; then
    say fail tmux "tmux -V exited $rc -- reinstall tmux on this node"
    return
  fi
  verdict=$(magent_tmux_grade "$out")
  version=${verdict#*$'\t'}
  floor=$(magent_tmux_floor)
  case ${verdict%%$'\t'*} in
    ok) say ok tmux "$version" ;;
    unread) say fail tmux "cannot read the tmux version ($version); magent needs tmux $floor or newer" ;;
    *) say fail tmux "$version is too old; magent needs tmux $floor or newer -- upgrade tmux on this node" ;;
  esac
}

check_claude_login() {
  local target=$1 out rc
  if ! command -v claude >/dev/null 2>&1; then
    say skip claude-login "claude is not installed"
    return
  fi
  out=$(bounded "$CLAUDE_PROBE_S" claude auth status --json 2>/dev/null)
  rc=$?
  if timed_out "$rc"; then
    say warn claude-login "claude auth status did not answer in ${CLAUDE_PROBE_S}s"
  elif [[ $out =~ \"loggedIn\"[[:space:]]*:[[:space:]]*true ]]; then
    say ok claude-login "logged in"
  else
    say fail claude-login "Claude Code is not logged in here -- run once: ssh $target claude"
  fi
}

check_github_key() {
  local out rc last
  if ! command -v ssh >/dev/null 2>&1; then
    say fail github-key "ssh is not on PATH -- install openssh-client on this node"
    return
  fi
  # Deliberate TOFU, the one write this doctor makes: accept-new records
  # github.com's host key in ~/.ssh/known_hosts on first contact, exactly as
  # the node's first git clone would. A CHANGED key is still refused.
  out=$(bounded "$GITHUB_PROBE_S" ssh -T -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=accept-new git@github.com 2>&1)
  rc=$?
  last=${out##*$'\n'}
  if timed_out "$rc"; then
    say fail github-key "ssh to github.com timed out after ${GITHUB_PROBE_S}s"
  elif [[ $out =~ Hi\ ([^!]+)! ]]; then
    say ok github-key "authenticates as ${BASH_REMATCH[1]}"
  elif [[ $out == *"Permission denied (publickey)"* ]]; then
    # The one failure setup repairs: it registers this user's key.
    say fail github-key "GitHub refused this node's key ($last) -- run: magent node setup"
  else
    # The key was never tried (DNS, a firewall, a reset): no key to blame.
    say fail github-key "could not reach GitHub over ssh (${last:-no output})"
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
  local root=$1 dir out rc avail
  # Only `~` and `~/...` are this user's home; `~bob/x` is not pasted onto it.
  case $root in
    \~) dir=$HOME ;;
    \~/*) dir=$HOME/${root#\~/} ;;
    *) dir=$root ;;
  esac
  while [ ! -d "$dir" ] && [ "$dir" != / ] && [ "$dir" != . ]; do
    dir=$(dirname "$dir")
  done
  out=$(bounded "$DF_PROBE_S" df -Pk "$dir" 2>/dev/null)
  rc=$?
  if timed_out "$rc"; then
    say warn disk "df did not answer in ${DF_PROBE_S}s under $root"
    return
  fi
  avail=$(printf '%s\n' "$out" | awk 'NR == 2 {print $4}')
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
  local out rc n=0
  out=$(bounded "$TMUX_PROBE_S" tmux -L "$MAGENT_SOCKET" list-sessions 2>/dev/null)
  rc=$?
  if timed_out "$rc"; then
    say warn sessions "tmux server on socket $MAGENT_SOCKET did not answer in ${TMUX_PROBE_S}s"
    return
  fi
  # No server yet (tmux exits 1, silent) is zero sessions, not a failure.
  [ -n "$out" ] && n=$(printf '%s\n' "$out" | wc -l)
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
  # Every probe runs under timeout(1): without it each would exit 127 and
  # read as its own wrong finding, so the one real cause is the only row.
  if ! command -v timeout >/dev/null 2>&1; then
    say fail doctor "timeout is not on PATH -- every probe runs under it; install coreutils on this node"
    return 0
  fi
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
