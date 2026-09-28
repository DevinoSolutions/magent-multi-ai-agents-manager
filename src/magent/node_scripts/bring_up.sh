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

# Every C0 control character is escaped (\u00XX), so the result line always
# parses whatever a path holds.
json_str() {
  local s=$1 c ch esc
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  if [[ $s == *[[:cntrl:]]* ]]; then
    for ((c = 1; c < 32; c++)); do
      printf -v ch "\\$(printf %03o "$c")"
      printf -v esc '\\u%04x' "$c"
      s=${s//"$ch"/$esc}
    done
  fi
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

# A url may carry user:token@; no message ever shows it.
redact_url() {
  local u=$1
  if [[ $u =~ ^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@(.*)$ ]]; then
    u="${BASH_REMATCH[1]}***@${BASH_REMATCH[2]}"
  fi
  printf '%s' "$u"
}

update_repo() {
  local url=$1 branch=$2 dir=$3 st refs head current
  local stuck="$dir: could not fast-forward (local changes or divergence): $branch to origin/$branch; reconcile it on the node"
  if [ -d "$dir/.git" ]; then
    if [ "$allow" != 1 ]; then
      st=$(git -C "$dir" status --porcelain --untracked-files=no) || die 5 "git status failed in $dir"
      [ -z "$st" ] || die 3 "$dir has uncommitted changes on the node; commit or discard them there, or pass --allow-dirty"
    fi
    git -C "$dir" fetch -q origin -- "$branch" || die 5 "git fetch failed in $dir"
    # Commits on a detached HEAD that no branch or tag holds would be orphaned
    # by the checkout below: refused whatever --allow-dirty says, losing
    # commits is never allowed. refs/stash does not count -- a later `git
    # stash drop` would lose the commit. A HEAD a branch or tag holds is
    # simply brought back.
    if ! current=$(git -C "$dir" symbolic-ref -q --short HEAD); then
      refs=$(git -C "$dir" for-each-ref --contains HEAD refs/heads refs/remotes refs/tags) ||
        die 5 "git for-each-ref failed in $dir"
      if [ -z "$refs" ]; then
        head=$(git -C "$dir" rev-parse --short HEAD) || die 5 "no HEAD in $dir"
        die 3 "$dir is on a detached HEAD at $head, a commit no branch or tag holds; put it on a branch there (git branch <name> $head) first"
      fi
    elif [ "$current" != "$branch" ]; then
      echo "magent: $dir was on $current; switching it to $branch" >&2
    fi
    if git -C "$dir" rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
      # Before the checkout: a refusal leaves the tree on its own branch.
      git -C "$dir" merge-base --is-ancestor "refs/heads/$branch" "refs/remotes/origin/$branch" || die 5 "$stuck"
      git -C "$dir" checkout -q "$branch" -- || die 5 "git checkout $branch failed in $dir"
      git -C "$dir" merge -q --ff-only "origin/$branch" || die 5 "$stuck"
    else
      git -C "$dir" checkout -q -b "$branch" --track "origin/$branch" ||
        die 5 "git checkout $branch failed in $dir"
    fi
  else
    mkdir -p -- "$(dirname -- "$dir")" || die 5 "cannot create the parent of $dir"
    git clone -q --branch "$branch" -- "$url" "$dir" || die 5 "git clone of $(redact_url "$url") failed"
  fi
  commits[$dir]=$(git -C "$dir" rev-parse HEAD) || die 5 "no HEAD in $dir"
}

# `capture VAR cmd...`: VAR = cmd's stdout minus the ONE newline realpath,
# dirname and mktemp end it with. A bare $(...) strips EVERY trailing newline,
# and a path may end in one -- `.env<LF>` would be resolved as `.env`.
capture() {
  local out
  out=$("${@:2}" && printf x) || return 1
  printf -v "$1" '%s' "${out%?x}"
}

# Create the folder $1 and every missing folder above it, each 0700, stated
# outright: a default ACL on a parent makes the kernel ignore the umask for a
# new folder (GitHub's runner homes carry one), and `mkdir -p -m` gives the
# mode to the last folder only. A folder already there is the node's own and
# keeps its mode.
mkdir_private() {
  local dir=$1
  local -a made=()
  while [ ! -e "$dir" ] && [ ! -h "$dir" ]; do
    made=("$dir" "${made[@]}")
    capture dir dirname -- "$dir" || return 1
  done
  for dir in "${made[@]}"; do
    mkdir -m 700 -- "$dir" || return 1
  done
  [ -d "$1" ]
}

# Copy every file under $1 into the EXISTING folder $2, mode 600, and set
# `copied` to their relative names. Every name is checked before anything is
# written. Each destination is resolved first, so a link already on the node
# (a folder or a file pointing out of $2) is refused, never written through;
# one that stays inside $2 is followed. A file is written to a fresh 0600 temp
# beside its target and renamed over it: an old 0644 file is replaced, never
# rewritten in place under a reader that holds it open. Folders created on the
# way are 0700 (mkdir_private) -- intended containment, not an accident.
copy_tree() {
  local src=$1 dest=$2 base rel target dir tmp
  local -a rels=()
  copied=()
  [ -d "$src" ] || return 0
  while IFS= read -r -d '' rel; do
    rel=${rel#./}
    [[ $rel != *[[:cntrl:]]* ]] || die 2 "payload member $(printf %q "$rel") has a control character in its name"
    rels+=("$rel")
  done < <(cd -- "$src" && find . -type f -print0 | sort -z)
  capture base realpath -e -- "$dest" || die 5 "cannot resolve $dest"
  for rel in "${rels[@]}"; do
    capture target realpath -m -- "$dest/$rel" || die 5 "cannot resolve $dest/$rel"
    [[ $target == "$base"/* ]] || die 5 "$dest/$rel resolves outside $dest ($target); not writing through a link"
    [ ! -d "$target" ] || die 5 "$dest/$rel is a folder on the node"
    capture dir dirname -- "$target" || die 5 "cannot resolve $dest/$rel"
    mkdir_private "$dir" || die 5 "cannot create a folder for $rel"
    capture tmp mktemp -- "$dir/.magent-ship.XXXXXX" || die 5 "cannot write $dest/$rel"
    # mktemp creates it 0600 whatever the umask; cp into it keeps that mode.
    cp -- "$src/$rel" "$tmp" || { rm -f -- "$tmp"; die 5 "cannot write $dest/$rel"; }
    mv -f -- "$tmp" "$target" || { rm -f -- "$tmp"; die 5 "cannot write $dest/$rel"; }
    copied+=("$rel")
  done
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
  mkdir_private "$dest" || die 5 "cannot create $dest"
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
  mkdir -- "$unpacked" || die 5 "mkdir failed"
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
    # A token git could read as an option (--upload-pack=...) is refused
    # here, before any git runs; the calls below also end their options.
    [[ $url != -* ]] || die 2 "a repo url may not start with -: $(redact_url "$url")"
    [[ $branch != -* ]] || die 2 "a branch may not start with -: $branch"
    [[ $dir == /* ]] || die 2 "a repo folder must be absolute: $dir"
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
  mkdir -p -- "$root" || die 5 "cannot create $root"
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
  mux new-session -d -e LANG=C.UTF-8 -s "$sid" -c "$root" -- "${cmd[@]}" || die 4 "tmux could not start session $sid"
  decorate "$unpacked/decorate"
  mux has-session -t "=$sid" 2>/dev/null || die 4 "session $sid exited as soon as it started"
  emit false
}

main "$@"; exit $?
