#!/usr/bin/env bash
# bring_up.sh -- run on a pool node by `magent up` / `--go` (PR-D), as
#   bash -s -- <socket> <up|push> <sid> <abs project root> <encoded claude project name>
# lib.sh reads <socket> into MAGENT_SOCKET and shifts it off (DECISION-26 ii),
# so main sees the rest.
# with a framed tar payload after the script (see remote_mux._payload):
#   header    NUL-terminated tokens (remote_mux._header)
#   decorate  tmux commands for the session's status line
#   project/  files that ship beside the clone
#   memory/   the project's Claude memory (seeded only if absent)
# Exit codes: 2 bad input, 3 dirty node tree, 4 tmux, 5 git or a write.
# The last stdout line is ONE JSON object; remote_mux._parse_result reads it.
set -euo pipefail
# @include lib.sh

declare -A commits=()
shipped=()
copied=()

die() {
  local code=$1
  shift
  echo "magent: $*" >&2
  exit "$code"
}

mux() { tmux -L "$MAGENT_SOCKET" "$@"; }

# The one step allowed to fail: the session is up either way, and a bare
# status line is not worth a failed bring-up under `set -e`.
decorate() {
  bash "$1" || echo "magent: status-line decoration failed (the session is up)" >&2
}

json_str() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\t'/\\t}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  printf '"%s"' "$s"
}

next_token() {
  IFS= read -r -d '' "$1" <&3 || die 2 "truncated payload header"
}

next_count() {
  next_token "$1"
  [[ ${!1} =~ ^[0-9]+$ ]] || die 2 "bad count in payload header"
}

emit() {
  local attached=$1 first=1 key rel
  printf '{"sid":%s,"attached_existing":%s,"cwd":%s,"commits":{' \
    "$(json_str "$sid")" "$attached" "$(json_str "$root")"
  for key in "${!commits[@]}"; do
    ((first)) || printf ','
    first=0
    printf '%s:%s' "$(json_str "$key")" "$(json_str "${commits[$key]}")"
  done
  printf '},"shipped":['
  first=1
  for rel in "${shipped[@]}"; do
    ((first)) || printf ','
    first=0
    printf '%s' "$(json_str "$rel")"
  done
  printf ']}\n'
}

# The PC checks every member name, and the node trusts none of them: GNU tar
# has no option that REFUSES an absolute name (it strips the `/` and extracts,
# exit 0) nor a link member, so the archive is listed and refused before a
# byte of it is written. Only plain files and folders, all relative, no `..`.
check_payload() {
  local tarball=$1 names types name kind
  names=$(tar -t -f "$tarball") || die 2 "unreadable payload"
  while IFS= read -r name; do
    case /$name/ in
      //* | */../*) die 2 "payload member $name would land outside its folder" ;;
    esac
  done <<<"$names"
  types=$(tar -tv -f "$tarball" | cut -c1) || die 2 "unreadable payload"
  while IFS= read -r kind; do
    [[ -z $kind || $kind == [-d] ]] || die 2 "payload carries a link or a special file"
  done <<<"$types"
}

need_tmux() {
  local version major minor
  command -v tmux >/dev/null 2>&1 || die 4 "tmux is not installed on this node (needs 3.2 or newer)"
  version=$(tmux -V) || die 4 "tmux -V failed on this node"
  [[ $version =~ ([0-9]+)\.([0-9]+) ]] || die 4 "cannot read the tmux version: $version (needs 3.2 or newer)"
  major=${BASH_REMATCH[1]}
  minor=${BASH_REMATCH[2]}
  if ((major < 3 || (major == 3 && minor < 2))); then
    die 4 "$version is too old; magent needs tmux 3.2 or newer"
  fi
}

update_repo() {
  local url=$1 branch=$2 dir=$3
  if [ -d "$dir/.git" ]; then
    if [ "$allow" != 1 ] && [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
      die 3 "$dir has uncommitted changes on the node; commit or discard them there, or pass --allow-dirty"
    fi
    git -C "$dir" fetch -q origin "$branch" || die 5 "git fetch failed in $dir"
    if git -C "$dir" rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
      git -C "$dir" checkout -q "$branch" || die 5 "git checkout $branch failed in $dir"
      git -C "$dir" merge -q --ff-only "origin/$branch" ||
        die 5 "$dir: $branch on the node has diverged from origin; reconcile it there"
    else
      git -C "$dir" checkout -q -b "$branch" --track "origin/$branch" ||
        die 5 "git checkout $branch failed in $dir"
    fi
  else
    mkdir -p "$(dirname "$dir")" || die 5 "cannot create the parent of $dir"
    git clone -q --branch "$branch" "$url" "$dir" || die 5 "git clone of $url failed"
  fi
  commits[$dir]=$(git -C "$dir" rev-parse HEAD) || die 5 "no HEAD in $dir"
}

# Copy every file under $1 into the EXISTING folder $2, mode 600, and set
# `copied` to their relative names. Each destination is resolved first, so a
# link already on the node (a folder or a file pointing out of $2) is refused,
# never written through; one that stays inside $2 is followed.
copy_tree() {
  local src=$1 dest=$2 base rel target mask
  copied=()
  [ -d "$src" ] || return 0
  base=$(realpath -e -- "$dest") || die 5 "cannot resolve $dest"
  mask=$(umask)
  umask 077
  while IFS= read -r -d '' rel; do
    rel=${rel#./}
    target=$(realpath -m -- "$dest/$rel") || die 5 "cannot resolve $dest/$rel"
    [[ $target == "$base"/* ]] || die 5 "$dest/$rel resolves outside $dest ($target); not writing through a link"
    [ ! -d "$target" ] || die 5 "$dest/$rel is a folder on the node"
    mkdir -p "$(dirname "$target")" || die 5 "cannot create a folder for $rel"
    cp "$src/$rel" "$target" || die 5 "cannot write $dest/$rel"
    chmod 600 "$target" || die 5 "cannot chmod $dest/$rel"
    copied+=("$rel")
  done < <(cd "$src" && find . -type f -print0 | sort -z)
  umask "$mask"
}

ship_files() {
  copy_tree "$1" "$2"
  shipped=("${copied[@]}")
}

# Present in ANY form is the node's own: a dangling link too (`-e` alone
# would follow it, and the seed would land wherever it points).
seed_memory() {
  local src=$1 dest=$2
  [ -d "$src" ] || return 0
  if [ -e "$dest" ] || [ -h "$dest" ]; then return 0; fi
  (umask 077 && mkdir -p "$dest") || die 5 "cannot create $dest"
  copy_tree "$src" "$dest"
}

# mode, sid, root, allow and work are deliberately global: emit, update_repo
# and the EXIT trap read them, and the trap runs after main has returned.
main() {
  [ $# -eq 4 ] || die 2 "usage: bring_up.sh <up|push> <sid> <root> <encoded name>"
  mode=$1
  sid=$2
  root=$3
  local enc=$4 user_umask unpacked magic nrepos nargv nfresh i url branch dir token
  case $mode in up | push) ;; *) die 2 "unknown mode: $mode" ;; esac
  [[ $enc =~ ^[A-Za-z0-9-]+$ ]] || die 2 "bad encoded project name: $enc"
  [[ $root == /* ]] || die 2 "the project root must be absolute: $root"
  # The payload carries secrets: nothing of it is ever readable by another
  # user here. The user's own umask is back before git and tmux run -- the
  # agent (and the tmux server it may start) must not inherit this one.
  user_umask=$(umask)
  umask 077
  work=$(mktemp -d) || die 5 "mktemp failed"
  trap 'rm -rf -- "$work"' EXIT
  unpacked=$work/unpacked
  mkdir "$unpacked" || die 5 "mkdir failed"
  magent_payload >"$work/payload.tar" || die 2 "unreadable payload"
  check_payload "$work/payload.tar"
  tar -x --no-same-owner --no-same-permissions -f "$work/payload.tar" -C "$unpacked" ||
    die 2 "unreadable payload"
  umask "$user_umask"
  [ -f "$unpacked/header" ] || die 2 "payload has no header"
  exec 3<"$unpacked/header"
  next_token magic
  [ "$magic" = MAGENT1 ] || die 2 "unknown payload header: $magic"
  next_token allow
  next_count nrepos
  local -a urls=() branches=() dirs=() argv=() fresh=()
  for ((i = 0; i < nrepos; i++)); do
    next_token url
    next_token branch
    next_token dir
    urls+=("$url")
    branches+=("$branch")
    dirs+=("$dir")
  done
  next_count nargv
  for ((i = 0; i < nargv; i++)); do next_token token; argv+=("$token"); done
  next_count nfresh
  for ((i = 0; i < nfresh; i++)); do next_token token; fresh+=("$token"); done
  exec 3<&-

  if [ "$mode" = push ]; then
    [ -d "$root" ] || die 5 "$root is not on this node yet; bring the project up first"
    ship_files "$unpacked/project" "$root"
    emit false
    return 0
  fi

  need_tmux
  if mux has-session -t "=$sid" 2>/dev/null; then
    decorate "$unpacked/decorate"
    emit true
    return 0
  fi
  ((nargv > 0)) || die 2 "no command to start"
  for ((i = 0; i < nrepos; i++)); do
    update_repo "${urls[i]}" "${branches[i]}" "${dirs[i]}"
  done
  mkdir -p "$root" || die 5 "cannot create $root"
  ship_files "$unpacked/project" "$root"
  seed_memory "$unpacked/memory" "$HOME/.claude/projects/$enc/memory"

  local -a cmd=("${argv[@]}") transcripts=()
  shopt -s nullglob
  transcripts=("$HOME/.claude/projects/$enc"/*.jsonl)
  shopt -u nullglob
  if ((nfresh > 0 && ${#transcripts[@]} == 0)); then
    cmd=("${fresh[@]}")
  fi
  # sshd hands a non-login command no locale; the agent's UI needs UTF-8
  # (DECISION-26 viii). `new-session -e` is tmux >= 3.2, checked above.
  mux new-session -d -e LANG=C.UTF-8 -s "$sid" -c "$root" "${cmd[@]}" || die 4 "tmux could not start session $sid"
  decorate "$unpacked/decorate"
  mux has-session -t "=$sid" 2>/dev/null || die 4 "session $sid exited as soon as it started"
  emit false
}

main "$@"; exit $?
