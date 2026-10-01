#!/usr/bin/env bash
# magent node doctor: health rows for this node user. Read-only: it writes
# nothing, and the two secrets it reads (the Claude token, git's github.com
# credential) reach curl on stdin only, never an argv or a row.
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
CLAUDE_PROBE_S=8  # claude auth status (local: which credential wins)
ANTHROPIC_PROBE_S=12  # the token's liveness check against the Anthropic API
CRED_PROBE_S=6  # git credential fill (runs gh's credential helper)
GITHUB_PROBE_S=12  # the credential's check against the GitHub API
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
# A version read that hangs, or exits non-zero, gets the same status as a
# missing tool: what a failing read printed is not a working tool's version.
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
    say "$missing" "$name" "$* exited $rc -- reinstall $name on this node"
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

# The file node_apply installs (node_apply.CLAUDE_TOKEN_FILE) and the one
# the session prelude reads (remote_mux.SESSION_AUTH_PRELUDE); pinned by test.
CLAUDE_TOKEN_FILE=.magent/claude-oauth-token
REFRESH_HINT="on the PC run: magent node auth refresh"

# api_answer <body+code>: split what `curl -w '\n%{http_code}'` printed into
# CODE and BODY (globals: bash functions cannot return two strings).
api_answer() {
  CODE=${1##*$'\n'}
  BODY=${1%$'\n'*}
  [[ $CODE =~ ^[0-9]{3}$ ]] || CODE=000
}

# The subscription token node setup shipped: there, owner-only, a token --
# then which credential Claude Code would actually use (an API key or an
# apiKeyHelper OUTRANKS the token, so sessions would bill it), then whether
# Anthropic still accepts it. The liveness call is count_tokens: no model
# runs, nothing is billed.
check_claude_auth() {
  local file=$HOME/$CLAUDE_TOKEN_FILE token="" mode out rc method source
  if ! command -v claude >/dev/null 2>&1; then
    say skip claude-auth "claude is not installed"
    return
  fi
  if [ -h "$file" ] || [ ! -f "$file" ]; then
    say fail claude-auth "no Claude subscription token on this node -- run: magent node setup"
    return
  fi
  mode=$(stat -c %a -- "$file" 2>/dev/null)
  if [ "$mode" != 600 ] || [ ! -O "$file" ]; then
    say fail claude-auth "the Claude token file is not owner-only (mode ${mode:-unknown}) -- run: magent node setup"
    return
  fi
  IFS= read -r token <"$file"
  if ! [[ $token =~ ^sk-ant-oat[A-Za-z0-9_-]{16,512}$ ]]; then
    say fail claude-auth "the file on this node is not a Claude subscription token -- $REFRESH_HINT"
    return
  fi
  # A subshell: the key variables are dropped and the token exported for
  # this one probe only, as the session prelude does -- never in an argv.
  out=$(
    unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
    export CLAUDE_CODE_OAUTH_TOKEN=$token
    bounded "$CLAUDE_PROBE_S" claude auth status --json 2>/dev/null
  )
  rc=$?
  if timed_out "$rc"; then
    say warn claude-auth "claude auth status did not answer in ${CLAUDE_PROBE_S}s"
    return
  fi
  # auth status names the winner: authMethod, plus apiKeySource whenever an
  # API key is configured at all (Claude Code 2.1's `auth status --json`).
  method=unknown source=""
  [[ $out =~ \"authMethod\"[[:space:]]*:[[:space:]]*\"([A-Za-z0-9_.]+)\" ]] && method=${BASH_REMATCH[1]}
  [[ $out =~ \"apiKeySource\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]] && source=${BASH_REMATCH[1]}
  if [ -n "$source" ] || [ "$method" = api_key ] || [ "$method" = api_key_helper ]; then
    say fail claude-auth "Claude Code here would bill an API key (${source:-$method}), not the subscription -- remove it from this user's Claude settings"
    return
  fi
  if [ "$method" = none ]; then
    say fail claude-auth "Claude Code here does not accept the shared token -- $REFRESH_HINT"
    return
  fi
  if [ "$method" != oauth_token ]; then
    say warn claude-auth "Claude Code here would authenticate with $method, not the shared subscription token"
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    say warn claude-auth "token in place; not checked with Anthropic: curl is not on PATH -- run: magent node setup"
    return
  fi
  out=$(
    printf 'Authorization: Bearer %s\n' "$token" |
      bounded "$ANTHROPIC_PROBE_S" curl -sS --connect-timeout 5 --max-time 10 \
        -w '\n%{http_code}' -H @- \
        -H 'anthropic-version: 2023-06-01' \
        -H 'anthropic-beta: oauth-2025-04-20,token-counting-2024-11-01' \
        -H 'content-type: application/json' \
        -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"ping"}]}' \
        'https://api.anthropic.com/v1/messages/count_tokens?beta=true' 2>/dev/null
  )
  rc=$?
  if timed_out "$rc"; then
    say warn claude-auth "the Anthropic API did not answer in ${ANTHROPIC_PROBE_S}s"
    return
  fi
  api_answer "$out"
  case $CODE in
    200 | 429) say ok claude-auth "subscription token accepted by Anthropic" ;;
    401) say fail claude-auth "Anthropic rejected this node's Claude token -- $REFRESH_HINT" ;;
    403)
      if [[ $BODY == *"revoked"* ]]; then
        say fail claude-auth "Anthropic rejected this node's Claude token (revoked) -- $REFRESH_HINT"
      else
        say warn claude-auth "could not confirm the Claude token with Anthropic (HTTP 403)"
      fi
      ;;
    000) say warn claude-auth "could not reach the Anthropic API from this node" ;;
    *) say warn claude-auth "could not confirm the Claude token with Anthropic (HTTP $CODE)" ;;
  esac
}

# git on this node reaches GitHub over https with the gh login node setup
# shipped (gh's credential helper; ssh remotes rewritten to https): ask git
# for its github.com credential exactly as a clone would, then ask GitHub
# whose it is. No ssh key is involved or required.
check_github() {
  local cred rc pass="" out rewrites form re=$'(^|\n)password=([^\n]*)'
  if ! command -v git >/dev/null 2>&1; then
    say fail github "git is not on PATH -- run: magent node setup"
    return
  fi
  cred=$(
    export GIT_TERMINAL_PROMPT=0
    printf 'protocol=https\nhost=github.com\n\n' |
      bounded "$CRED_PROBE_S" git credential fill 2>/dev/null
  )
  rc=$?
  if timed_out "$rc"; then
    say warn github "git credential fill did not answer in ${CRED_PROBE_S}s"
    return
  fi
  [ "$rc" -eq 0 ] && [[ $cred =~ $re ]] && pass=${BASH_REMATCH[2]}
  if [ -z "$pass" ]; then
    say fail github "git has no github.com login -- run: magent node setup"
    return
  fi
  rewrites=$(git config --global --get-all url.https://github.com/.insteadOf 2>/dev/null)
  for form in git@github.com: ssh://git@github.com/; do
    if [[ $'\n'$rewrites$'\n' != *$'\n'"$form"$'\n'* ]]; then
      say warn github "github.com ssh remotes are not pointed at https -- run: magent node setup"
      return
    fi
  done
  if ! command -v curl >/dev/null 2>&1; then
    say warn github "git has a github.com login; not checked with GitHub: curl is not on PATH -- run: magent node setup"
    return
  fi
  out=$(
    printf 'Authorization: token %s\n' "$pass" |
      bounded "$GITHUB_PROBE_S" curl -sS --connect-timeout 5 --max-time 10 \
        -w '\n%{http_code}' -H @- -H 'User-Agent: magent-node-doctor' \
        https://api.github.com/user 2>/dev/null
  )
  rc=$?
  if timed_out "$rc"; then
    say warn github "the GitHub API did not answer in ${GITHUB_PROBE_S}s"
    return
  fi
  api_answer "$out"
  case $CODE in
    200)
      if [[ $BODY =~ \"login\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
        say ok github "git authenticates as ${BASH_REMATCH[1]}"
      else
        say ok github "git authenticates to github.com"
      fi
      ;;
    401) say fail github "GitHub rejected the gh login git uses -- on the PC run: gh auth login, then magent node setup" ;;
    000) say warn github "could not reach the GitHub API from this node" ;;
    *) say warn github "could not confirm git's github.com login (HTTP $CODE)" ;;
  esac
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
  local root=$HOME
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --root | --target)
        if [ "$#" -lt 2 ]; then
          say fail doctor "$1 needs a value"
          return 0
        fi
        # --target is still accepted (older PCs pass it) and no longer read:
        # no row asks the user to ssh in by hand.
        [ "$1" = --root ] && root=$2
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
  check_claude_auth
  check_github
  check_locale
  check_disk "$root"
  check_sessions
  return 0
}

main "$@"; exit $?
