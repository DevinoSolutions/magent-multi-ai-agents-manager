#!/usr/bin/env bash
# magent node setup: the one root hop that makes a machine a node.
# Fed over stdin by remote_mux.setup_node as root (`bash -s -- <socket>
# <user>...`; lib.sh takes the socket, which setup does not use); the payload
# after the sentinel is this PC's ssh public key, one line.
# Idempotent: every step prints ok/did/skip/fail rows, and a second run prints
# only skip rows plus one `key` row per user (that user's node GitHub key).
# Root does only what needs root -- packages, the account, the docker group.
# Everything under a user's home is written AS that user (the user phase), so
# a path the user controls can never aim a root write somewhere else.
set -euo pipefail
# @include lib.sh
# @include tmux_floor.sh

# openssh-client: the user phase runs ssh-keygen, and a minimal image may not
# ship it.
PACKAGES=(tmux git curl python3 ca-certificates openssh-client)
USER_RE='^[a-z_][a-z0-9_-]{0,31}$'
KEY_RE='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^[:cntrl:]]*)?$'
GH_KEYRING=/etc/apt/keyrings/githubcli-archive-keyring.gpg
GH_LIST=/etc/apt/sources.list.d/github-cli.list

# main runs every step as `step || rc=1`, which switches set -e off inside it:
# so each step chains its own writes and reports its own fail row.

say() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }

# version_row <status> <item> <tool> [<prefix>]: say <status> <item> with the
# first line of `<tool> --version`, under timeout(1) -- a hung binary is its
# own fail row, not a hung setup (124: TERM ended it, 137: KILL did).
version_row() {
  local s=4 k=2 out rc=0
  out=$(timeout -k "$k" "$s" "$3" --version 2>/dev/null) || rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    say fail "$2" "$3 --version timed out after ${s}s"
    return 1
  fi
  say "$1" "$2" "${4:-}${out%%$'\n'*}"
}

# Installed means dpkg's "ii": `dpkg -s` also succeeds for a package removed
# with its config files left behind (state "rc").
installed() {
  [ "$(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null)" = "ii " ]
}

step_packages() {
  local p out missing=()
  for p in "${PACKAGES[@]}"; do
    installed "$p" || missing+=("$p")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    say skip packages "${PACKAGES[*]}"
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    say fail packages "no apt-get here: magent node setup supports Debian/Ubuntu nodes"
    return 1
  fi
  if out=$( { DEBIAN_FRONTEND=noninteractive apt-get update -qq &&
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"; } 2>&1 ); then
    say did packages "installed ${missing[*]}"
  else
    say fail packages "apt-get could not install ${missing[*]}: ${out##*$'\n'}"
    return 1
  fi
}

# DECISION-22: apt "installs" whatever the release ships (Ubuntu 20.04: 3.0a),
# and D's bring_up.sh refuses anything under the floor. Refuse it here, by
# version, so setup never reports a node bring_up cannot use.
step_tmux() {
  local verdict version floor
  verdict=$(magent_tmux_verdict)
  version=${verdict#*$'\t'}
  floor=$(magent_tmux_floor)
  case ${verdict%%$'\t'*} in
    ok)
      say ok tmux "$version (needs $floor or newer)"
      ;;
    missing)
      say fail tmux "tmux is not installed; magent needs tmux $floor or newer"
      return 1
      ;;
    unread)
      say fail tmux "cannot read the tmux version ($version); magent needs tmux $floor or newer"
      return 1
      ;;
    *)
      say fail tmux "$version is too old; magent needs tmux $floor or newer (Ubuntu 22.04 ships 3.2a) -- upgrade tmux on this node, then run magent node setup again"
      return 1
      ;;
  esac
}

step_gh() {
  local out
  if command -v gh >/dev/null 2>&1; then
    version_row skip gh gh
    return
  fi
  # gh's official apt repository (cli/cli docs/install_linux.md). One that
  # cannot be read is taken out again: a dead source would fail every later
  # `apt-get update` on this node, the owner's as well as magent's.
  if ! out=$( { { [ -d /etc/apt/keyrings ] || mkdir -m 755 /etc/apt/keyrings; } &&
      curl -fsSL -o "$GH_KEYRING" https://cli.github.com/packages/githubcli-archive-keyring.gpg &&
      chmod go+r "$GH_KEYRING" &&
      arch=$(dpkg --print-architecture) &&
      printf 'deb [arch=%s signed-by=%s] https://cli.github.com/packages stable main\n' \
        "$arch" "$GH_KEYRING" > "$GH_LIST" &&
      DEBIAN_FRONTEND=noninteractive apt-get update -qq; } 2>&1 ); then
    rm -f -- "$GH_LIST" "$GH_KEYRING"
    say fail gh "could not add gh's apt repository: ${out##*$'\n'}"
    return 1
  fi
  if out=$(DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gh 2>&1); then
    version_row did gh gh "installed "
  else
    say fail gh "apt-get could not install gh: ${out##*$'\n'}"
    return 1
  fi
}

step_user() {
  local u=$1 out
  if getent passwd "$u" >/dev/null 2>&1; then
    say skip "user:$u" "exists"
    return 0
  fi
  if out=$(useradd --create-home --shell /bin/bash "$u" 2>&1); then
    say did "user:$u" "created"
  else
    say fail "user:$u" "useradd: ${out##*$'\n'}"
    return 1
  fi
}

step_docker() {
  local u=$1 line
  if ! line=$(getent group docker 2>/dev/null); then
    say skip "docker:$u" "no docker group on this node"
    return 0
  fi
  # awk reads every member (never `grep -q`, whose early exit is SIGPIPE
  # under pipefail).
  if printf '%s\n' "$line" | cut -d: -f4 | tr ',' '\n' |
      awk -v u="$u" '$0 == u {f = 1} END {exit !f}'; then
    say skip "docker:$u" "already in the docker group"
    return 0
  fi
  if usermod -aG docker "$u"; then
    say did "docker:$u" "added to the docker group"
  else
    say fail "docker:$u" "usermod -aG docker $u failed"
    return 1
  fi
}

# The user phase runs AS the user, in a bash login shell, through runuser: it
# sees only the functions shipped to it with `declare -f`, and no set -e.
# A symlinked ~/.ssh or authorized_keys is refused, not followed: magent does
# not write through a link it did not make. (`test -h`, not its `-L` alias:
# B's socket pin reads the word after every `-L` in a script as a socket.)
# shellcheck disable=SC2317  # invoked through `declare -f` in run_user_phase
user_authorized() {
  local u=$1 key=$2 ssh="$HOME/.ssh" ak="$HOME/.ssh/authorized_keys" rest t b
  if [ -h "$ssh" ] || [ -h "$ak" ]; then
    say fail "authorized_keys:$u" "$u's .ssh or its authorized_keys is a symlink; magent does not write through it"
    return 1
  fi
  t=${key%% *}
  rest=${key#* }
  b=${rest%% *}
  # Authorized = a line that is not a comment carries this key's type and blob
  # as two adjacent whole fields (after any options).
  if [ -f "$ak" ] && awk -v t="$t" -v b="$b" '
      /^[[:space:]]*(#|$)/ {next}
      {for (i = 1; i < NF; i++) if ($i == t && $(i + 1) == b) {found = 1; exit}}
      END {exit !found}' "$ak" 2>/dev/null; then
    say skip "authorized_keys:$u" "this PC's key is already authorized"
    return 0
  fi
  # A last line without its newline would glue the new key onto it.
  if ! { mkdir -p "$ssh" && chmod 700 "$ssh" &&
      { [ ! -s "$ak" ] || [ -z "$(tail -c1 "$ak")" ] || printf '\n' >> "$ak"; } &&
      printf '%s\n' "$key" >> "$ak" && chmod 600 "$ak"; } 2>/dev/null; then
    say fail "authorized_keys:$u" "could not write ~/.ssh/authorized_keys"
    return 1
  fi
  say did "authorized_keys:$u" "this PC's key authorized"
}

# shellcheck disable=SC2317  # invoked through `declare -f` in run_user_phase
user_claude() {
  local u=$1 out tmp
  if command -v claude >/dev/null 2>&1; then
    version_row skip "claude:$u" claude
    return
  fi
  # Downloaded whole, then run: `curl | bash` hands bash half a script when
  # the connection drops mid-transfer. The installer keeps its usual umask.
  if ! tmp=$(mktemp 2>/dev/null); then
    say fail "claude:$u" "mktemp failed: nowhere to download the Claude installer"
    return 1
  fi
  out=$( { curl -fsSL -o "$tmp" https://claude.ai/install.sh &&
      (umask 022 && bash "$tmp"); } 2>&1 ) || true
  rm -f -- "$tmp"
  if command -v claude >/dev/null 2>&1; then
    version_row did "claude:$u" claude
  else
    say fail "claude:$u" "the Claude installer did not put claude on PATH: ${out##*$'\n'}"
    return 1
  fi
}

# Never a second node key over the first: GitHub may already hold it. A lost
# .pub is derived again from the private key.
# The key is chmod 600 on every run and again once generated: ssh-keygen's 0600
# is umask 077 over open(0644) (OpenSSH >= 8.2), which a default ACL on ~/.ssh
# overrides, and ssh (and `ssh-keygen -y`) refuses a key others can read.
# Until that chmod, only ~/.ssh keeps others from a new key, so ~/.ssh is made
# 0700 first: `mkdir -p` under that same ACL makes it 0777.
# shellcheck disable=SC2317  # invoked through `declare -f` in run_user_phase
user_node_key() {
  local u=$1 out pub mode pad made="" ssh="$HOME/.ssh" id="$HOME/.ssh/id_ed25519"
  if [ -h "$ssh" ] || [ -h "$id" ]; then
    say fail "node-key:$u" "$u's .ssh or its id_ed25519 is a symlink; magent does not write through it"
    return 1
  fi
  # The row says whether the chmod changed anything (GNU stat: nodes are
  # Debian/Ubuntu). A mode stat cannot read is a repair, never a clean skip.
  # A fail row after it carries the note too: the next run reads 0600.
  if [ -f "$id" ]; then
    mode=$(stat -c %a "$id" 2>/dev/null) || mode=""
    if ! chmod 600 "$id" 2>/dev/null; then
      say fail "node-key:$u" "could not make ~/.ssh/id_ed25519 owner-only (0600); ssh refuses an open key"
      return 1
    fi
    # %a drops leading zeros (0 for 0000): the old mode reads four digits
    # wide, and only a group or other bit (the last two) was an exposure.
    pad=000$mode
    case $mode in
      600) ;;
      "") made="id_ed25519 made owner-only (0600): its earlier mode could not be read" ;;
      *)
        if [ "${pad: -2}" = 00 ]; then
          made="id_ed25519 set to 0600 (it was ${pad: -4}, already owner-only)"
        else
          made="id_ed25519 made owner-only: it was ${pad: -4}, now 0600"
        fi
        ;;
    esac
  fi
  # A dangling .pub link reads as no .pub (`-f` is false): the derive and the
  # generation below would create its target. A live link is only read.
  if [ -h "$id.pub" ] && [ ! -e "$id.pub" ]; then
    say fail "node-key:$u" "$u's id_ed25519.pub is a dangling symlink; magent does not write through it${made:+; $made}"
    return 1
  elif [ -f "$id.pub" ]; then
    # A .pub alone would send GitHub a key this node cannot clone with.
    if [ ! -f "$id" ]; then
      say fail "node-key:$u" "id_ed25519.pub in ~/.ssh has no private key beside it; remove the .pub and rerun"
      return 1
    elif [ -n "$made" ]; then
      say did "node-key:$u" "$made"
    else
      say skip "node-key:$u" "id_ed25519 already in ~/.ssh"
    fi
  elif ! command -v ssh-keygen >/dev/null 2>&1; then
    say fail "node-key:$u" "ssh-keygen is not installed (Debian/Ubuntu package openssh-client)${made:+; $made}"
    return 1
  elif [ -f "$id" ]; then
    if ! out=$(ssh-keygen -y -P "" -f "$id" 2>&1 > "$id.pub"); then
      rm -f -- "$id.pub"
      say fail "node-key:$u" "ssh-keygen -y: ${out##*$'\n'}${made:+; $made}"
      return 1
    fi
    say did "node-key:$u" "id_ed25519.pub derived again from the private key in ~/.ssh${made:+; $made}"
  elif ! { { [ -d "$ssh" ] || mkdir -m 700 "$ssh"; } && chmod 700 "$ssh"; } 2>/dev/null; then
    say fail "node-key:$u" "could not make ~/.ssh owner-only (0700) for the new key"
    return 1
  elif out=$(ssh-keygen -q -t ed25519 -N "" -C "magent@$(hostname)" -f "$id" 2>&1); then
    if ! chmod 600 "$id" 2>/dev/null; then
      say fail "node-key:$u" "could not make the new ~/.ssh/id_ed25519 owner-only (0600); ssh refuses an open key"
      return 1
    fi
    say did "node-key:$u" "id_ed25519 generated in ~/.ssh (the private key never leaves this node)"
  else
    say fail "node-key:$u" "ssh-keygen: ${out##*$'\n'}"
    return 1
  fi
  if ! pub=$(cat "$id.pub" 2>/dev/null) || [ -z "$pub" ]; then
    say fail "node-key:$u" "no readable ~/.ssh/id_ed25519.pub"
    return 1
  fi
  say key "$u" "$pub"
}

# shellcheck disable=SC2317  # invoked through `declare -f` in run_user_phase
user_phase() {
  local u=$1 key=$2 rc=0
  umask 077
  export PATH="$HOME/.local/bin:$PATH"
  user_authorized "$u" "$key" || rc=1
  user_claude "$u" || rc=1
  user_node_key "$u" || rc=1
  return "$rc"
}

# --shell: the phase is bash functions, whatever the account's login shell is.
# The login shell sources the user's profile first, and what it prints lands
# here too: only this user's own rows get through (`*:<user>`, or the `key`
# row for <user>). keys() is last-wins, so a forged `key` row would otherwise
# replace the key GitHub gets for another user.
run_user_phase() {
  local u=$1 key=$2
  local -a st
  runuser --login --shell=/bin/bash \
    --command="$(declare -f say version_row user_phase user_authorized user_claude user_node_key); user_phase $(printf '%q %q' "$u" "$key")" \
    "$u" |
    awk -F'\t' -v u="$u" '$1 == "key" ? $2 == u : substr($2, length($2) - length(u)) == ":" u'
  st=("${PIPESTATUS[@]}")
  ((st[1] == 0)) || return 1
  return "${st[0]}"
}

main() {
  local u key entry uid uid_min uid_max rc=0
  if [ "$#" -eq 0 ]; then
    say fail setup "no user named: magent node setup <nick> --user <name>"
    return 2
  fi
  uid_min=$(awk '/^UID_MIN[[:space:]]/ {print $2; exit}' /etc/login.defs 2>/dev/null) || uid_min=""
  [[ $uid_min =~ ^[0-9]+$ ]] || uid_min=1000
  uid_max=$(awk '/^UID_MAX[[:space:]]/ {print $2; exit}' /etc/login.defs 2>/dev/null) || uid_max=""
  [[ $uid_max =~ ^[0-9]+$ ]] || uid_max=60000
  # Every name is checked before anything changes: a node user is a person's
  # own account -- never root, never an existing account setup's own useradd
  # could not have made (it allocates in [UID_MIN, UID_MAX]).
  for u in "$@"; do
    if ! [[ $u =~ $USER_RE ]]; then
      say fail setup "not a valid Unix user name: $(printf '%q' "$u")"
      return 2
    fi
    if [ "$u" = root ]; then
      say fail setup "root is not a node user: name a person's own account"
      return 2
    fi
    if entry=$(getent passwd "$u" 2>/dev/null); then
      uid=$(printf '%s\n' "$entry" | cut -d: -f3)
      if ! [[ $uid =~ ^[0-9]+$ ]] || ((10#$uid < 10#$uid_min || 10#$uid > 10#$uid_max)); then
        say fail setup "$u is not a person's account (uid $uid, outside UID_MIN $uid_min to UID_MAX $uid_max): name a person's own account"
        return 2
      fi
      # The kernel's overflow id (nobody/nfsnobody), even under a login.defs
      # whose UID_MAX reaches it.
      if ((10#$uid == 65534)); then
        say fail setup "$u is the overflow account (uid 65534): name a person's own account"
        return 2
      fi
    fi
  done
  key=$(magent_payload)
  key=${key%$'\r'}
  # Never echo the payload: a mistaken paste may be a PRIVATE key.
  if [[ $key == *$'\n'* ]] || ! [[ $key =~ $KEY_RE ]]; then
    say fail key "the payload is not one ssh public key line"
    return 2
  fi
  exec </dev/null
  if [ "$(id -u)" != 0 ]; then
    say fail setup "not root: magent node setup connects as root@<host> for this one hop"
    return 1
  fi
  # Every version probe runs under timeout(1): without it each would exit 127
  # and read as its own wrong finding, so the one real cause is the only row.
  if ! command -v timeout >/dev/null 2>&1; then
    say fail setup "timeout is not on PATH -- every probe runs under it; install coreutils on this node"
    return 1
  fi
  step_packages || rc=1
  step_tmux || rc=1
  step_gh || rc=1
  for u in "$@"; do
    step_user "$u" || { rc=1; continue; }
    step_docker "$u" || rc=1
    run_user_phase "$u" "$key" || rc=1
  done
  return "$rc"
}

main "$@"; exit $?
