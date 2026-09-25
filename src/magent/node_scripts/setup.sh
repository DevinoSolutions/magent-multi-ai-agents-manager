#!/usr/bin/env bash
# magent node setup: the one root hop that makes a machine a node.
# Fed over stdin by remote_mux.setup_node as root (`bash -s -- <socket>
# <user>...`; lib.sh takes the socket, which setup does not use); the payload
# after the sentinel is this PC's ssh public key, one line.
# Idempotent: every step prints ok/did/skip/fail rows, and a second run prints
# only skip rows plus one `key` row per user (that user's node GitHub key).
set -euo pipefail
# @include lib.sh
# @include tmux_floor.sh

PACKAGES=(tmux git curl python3 ca-certificates)
USER_RE='^[a-z_][a-z0-9_-]{0,31}$'
KEY_RE='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^[:cntrl:]]*)?$'
GH_KEYRING=/etc/apt/keyrings/githubcli-archive-keyring.gpg

say() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }

step_packages() {
  local p out missing=()
  for p in "${PACKAGES[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
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
    say skip gh "$(gh --version 2>/dev/null | head -n1)"
    return 0
  fi
  # gh's official apt repository (cli/cli docs/install_linux.md).
  if out=$( { mkdir -p -m 755 /etc/apt/keyrings &&
      curl -fsSL -o "$GH_KEYRING" https://cli.github.com/packages/githubcli-archive-keyring.gpg &&
      chmod go+r "$GH_KEYRING" &&
      printf 'deb [arch=%s signed-by=%s] https://cli.github.com/packages stable main\n' \
        "$(dpkg --print-architecture)" "$GH_KEYRING" > /etc/apt/sources.list.d/github-cli.list &&
      DEBIAN_FRONTEND=noninteractive apt-get update -qq &&
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gh; } 2>&1 ); then
    say did gh "installed $(gh --version 2>/dev/null | head -n1)"
  else
    say fail gh "could not install gh: ${out##*$'\n'}"
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

step_authorized() {
  local u=$1 key=$2 home blob ak
  if ! home=$(getent passwd "$u" | cut -d: -f6) || [ -z "$home" ]; then
    say fail "authorized_keys:$u" "no home directory for $u"
    return 1
  fi
  ak="$home/.ssh/authorized_keys"
  blob=$(printf '%s\n' "$key" | awk '{print $2}')
  if [ -f "$ak" ] && awk -v b="$blob" 'index($0, b) {f = 1} END {exit !f}' "$ak"; then
    say skip "authorized_keys:$u" "this PC's key is already authorized"
    return 0
  fi
  mkdir -p "$home/.ssh"
  chmod 700 "$home/.ssh"
  # A last line without its newline would glue the new key onto it.
  if [ -s "$ak" ] && [ -n "$(tail -c1 "$ak")" ]; then printf '\n' >> "$ak"; fi
  printf '%s\n' "$key" >> "$ak"
  chmod 600 "$ak"
  chown -R "$u:" "$home/.ssh"
  say did "authorized_keys:$u" "this PC's key authorized"
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

# Runs AS the user, in a login shell, through runuser: it sees only the
# functions shipped to it with `declare -f`.
user_phase() {
  local u=$1 out rc=0
  export PATH="$HOME/.local/bin:$PATH"
  if command -v claude >/dev/null 2>&1; then
    say skip "claude:$u" "$(claude --version 2>/dev/null | head -n1)"
  else
    out=$( { curl -fsSL https://claude.ai/install.sh | bash; } 2>&1 ) || true
    if command -v claude >/dev/null 2>&1; then
      say did "claude:$u" "$(claude --version 2>/dev/null | head -n1)"
    else
      say fail "claude:$u" "the Claude installer did not put claude on PATH: ${out##*$'\n'}"
      rc=1
    fi
  fi
  if [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
    say skip "node-key:$u" "id_ed25519 already in ~/.ssh"
  else
    mkdir -p -m 700 "$HOME/.ssh"
    if out=$(ssh-keygen -q -t ed25519 -N "" -C "magent@$(hostname)" -f "$HOME/.ssh/id_ed25519" 2>&1); then
      say did "node-key:$u" "id_ed25519 generated in ~/.ssh (the private key never leaves this node)"
    else
      say fail "node-key:$u" "ssh-keygen: ${out##*$'\n'}"
      return 1
    fi
  fi
  say key "$u" "$(cat "$HOME/.ssh/id_ed25519.pub")"
  return "$rc"
}

run_user_phase() {
  local u=$1
  runuser --login --command="$(declare -f say user_phase); user_phase $(printf '%q' "$u")" "$u"
}

main() {
  local u key rc=0
  if [ "$#" -eq 0 ]; then
    say fail setup "no user named: magent node setup <nick> --user <name>"
    return 2
  fi
  for u in "$@"; do
    if ! [[ $u =~ $USER_RE ]]; then
      say fail setup "not a valid Unix user name: $u"
      return 2
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
  step_packages || rc=1
  step_tmux || rc=1
  step_gh || rc=1
  for u in "$@"; do
    step_user "$u" || { rc=1; continue; }
    step_authorized "$u" "$key" || rc=1
    step_docker "$u" || rc=1
    run_user_phase "$u" || rc=1
  done
  return "$rc"
}

main "$@"; exit $?
