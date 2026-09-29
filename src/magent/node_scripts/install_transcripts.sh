#!/usr/bin/env bash
# install_transcripts.sh <encoded name> -- stdin carries this script, the line
# __MAGENT_PAYLOAD__, then a tar of a Claude project directory's CONTENTS
# (<uuid>.jsonl, <uuid>/subagents/, memory/). They are installed into
# ~/.claude/projects/<encoded name>/, whose physical path is printed LAST.
#
# Never extracted in place. The whole payload lands in a private temp dir
# beside the destination first; a missing or broken payload (tar fails) exits
# 3 having touched nothing. Then each regular file is placed:
#   - not on the node yet            -> moved in
#   - byte-identical on the node     -> left alone
#   - the node's copy is a strict byte prefix of the incoming one (the
#     conversation grew here since the node's copy was written) -> replaced
#   - anything else (the node's copy is longer or has diverged: the node kept
#     working) -> the NODE's copy is kept, and "KEPT<TAB><relative path>" is
#     printed so magent can say so
# A node FILE where the payload has a directory is KEPT (reported once), and
# every payload file beneath it is KEPT too; the rest still lands, exit 0.
# Nothing is ever deleted. Everything it creates is owner-only by an explicit
# mode, never by the umask alone: a default ACL on the node user's home makes
# the kernel ignore the umask, and tar gives each item the mode its ARCHIVE
# entry names (0666/0777 from a Windows PC or an older magent). So every
# payload item is chmodded (folders 0700, files 0600) before anything moves,
# and every folder this script makes is private_dir's. tar never restores
# the payload's owner.
#
# Exit codes: 2 the name is outside the encoder's alphabet; 3 the payload is
# missing or broken; 4 the destination (or a directory inside it the payload
# needs) is a symlink, which could point anywhere; 5 a folder it needs (the
# temp dir included) could not be made (a node user with no home, say) or
# restricted to its owner, before any file is placed; 6 a file could not be
# moved into place, and the files placed before it stay. Each refusal is said
# in this script's words, and the failed command's own reason follows on a
# line of its own, tagged by said_why: remote_mux logs that line and never
# shows it.
#
# The name was computed by magent's one encoder (nodes.encoded_project_dir);
# this script only refuses anything outside that encoder's alphabet, so a bad
# argument can never write outside ~/.claude/projects. The tmux socket arrives
# as $1 and lib.sh shifts it off (DECISION-26 ii); the payload is read with
# lib.sh's magent_payload.
set -euo pipefail
# @include lib.sh

# Global, not local: the EXIT trap runs after main returns, and under `set -u`
# a trap naming main's local would fail.
tmp=""

# Is file $1 a strict byte prefix of file $2? wc's output is padded on BSD;
# the arithmetic strips it.
is_prefix() {
  local have want
  have=$(($(wc -c <"$1")))
  want=$(($(wc -c <"$2")))
  [ "$have" -lt "$want" ] && head -c "$have" "$2" | cmp -s - "$1"
}

# Is some ancestor of relative path $2 (not $2 itself) a non-directory under
# $1? find lists a directory before its children, so that ancestor has
# already been reported KEPT, and private_dir beneath it would fail.
under_a_file() {
  local up="$2"
  while [ "${up%/*}" != "$up" ]; do
    up="${up%/*}"
    if [ -e "$1/$up" ] && [ ! -d "$1/$up" ]; then
      return 0
    fi
  done
  return 1
}

# Tag the OS's reason for a refusal: the first line of a failed command's own
# words ($1), after its last ": " -- where GNU and BSD tools both put it. The
# first, because a tool that says more than one line says the cause first
# and a summary after it (tar's "Exiting with failure status"). One line
# under remote_mux.INSTALL_REASON_TAG, which magent logs and never shows.
said_why() {
  local nl=$'\n' why
  why=${1%%$nl*}
  printf 'install_transcripts.sh: reason: %s\n' "${why##*: }" >&2
}

# Make folder $1 owner-only unless it is there already (the node's own is
# left as it is); its parent must exist. A default ACL can only narrow
# mkdir's mode, never widen it, and the chmod makes it exact. A mkdir that
# fails with the folder there after all lost a race to another install,
# whose private_dir made it: success, as mkdir -p had it, and not a word.
# Any other failure -- no folder, or one made that the chmod could not
# restrict -- is said in this script's words and returns 1; each caller
# refuses with exit 5. mkdir's and chmod's own words are the OS's words:
# nodes.log's, never the screen's -- said_why tags their reason for the log,
# and nothing else of them is printed. chmod's -- comes before
# the mode: BSD chmod (macOS) stops reading options at the mode, so a --
# after it is a file.
private_dir() {
  local why
  [ -d "$1" ] && return 0
  if why=$(mkdir -m 700 -- "$1" 2>&1); then
    why=$(chmod -- 700 "$1" 2>&1) && return 0
    printf 'install_transcripts.sh: cannot restrict folder %s to its owner; no file installed\n' \
      "$1" >&2
  elif [ -d "$1" ]; then
    return 0
  else
    printf 'install_transcripts.sh: cannot make folder %s; no file installed\n' "$1" >&2
  fi
  said_why "$why"
  return 1
}

# Refuse the install at file $1 (relative to the destination), mv's words in
# $2: the files placed before it stay, as a KEPT file does.
cannot_place() {
  printf 'install_transcripts.sh: cannot move %s into place; the files placed before it stay\n' \
    "$1" >&2
  said_why "$2"
}

main() {
  local name="${1:-}" projects dest src rel target why
  umask 077
  case "$name" in
    "" | *[!A-Za-z0-9-]*)
      printf 'install_transcripts.sh: not an encoded project dir name: %s\n' "$name" >&2
      return 2
      ;;
  esac
  projects="$HOME/.claude/projects"
  dest="$projects/$name"
  if [ -h "$dest" ]; then
    printf 'install_transcripts.sh: %s is a symlink; not installing through it\n' "$dest" >&2
    return 4
  fi
  private_dir "$HOME/.claude" || return 5
  private_dir "$projects" || return 5
  if ! why=$(mktemp -d "$projects/.magent-install.XXXXXX" 2>&1); then
    printf 'install_transcripts.sh: cannot make a temp folder in %s; no file installed\n' \
      "$projects" >&2
    said_why "$why"
    return 5
  fi
  tmp=$why
  trap 'rm -rf "$tmp"' EXIT
  # tar's words, and magent_payload's for no sentinel line, are the OS's.
  if ! why=$({ magent_payload | tar --no-same-owner --no-same-permissions -xf - -C "$tmp"; } 2>&1); then
    printf 'install_transcripts.sh: the payload is missing or broken; nothing installed\n' >&2
    said_why "$why"
    return 3
  fi
  # Owner-only whatever the archive said, before anything moves: the
  # folders, then the files.
  if ! why=$(find "$tmp" -type d -exec chmod 700 {} + 2>&1) ||
    ! why=$(find "$tmp" -type f -exec chmod 600 {} + 2>&1); then
    printf 'install_transcripts.sh: cannot restrict the payload to its owner; no file installed\n' >&2
    said_why "$why"
    return 5
  fi

  # Every directory the payload needs, checked before anything is placed.
  while IFS= read -r -d '' src; do
    if [ -h "$dest/${src#"$tmp"/}" ]; then
      printf 'install_transcripts.sh: %s is a symlink; not installing through it\n' \
        "$dest/${src#"$tmp"/}" >&2
      return 4
    fi
  done < <(find "$tmp" -mindepth 1 -type d -print0)

  private_dir "$dest" || return 5
  while IFS= read -r -d '' src; do
    rel="${src#"$tmp"/}"
    if under_a_file "$dest" "$rel"; then
      :  # its ancestor was KEPT already; the files under it are KEPT below
    elif [ -e "$dest/$rel" ] && [ ! -d "$dest/$rel" ]; then
      printf 'KEPT\t%s\n' "$rel"  # a file where the payload has a directory
    else
      # Its parent came earlier: find lists a folder before what is in it.
      private_dir "$dest/$rel" || return 5
    fi
  done < <(find "$tmp" -mindepth 1 -type d -print0)

  while IFS= read -r -d '' src; do
    rel="${src#"$tmp"/}"
    target="$dest/$rel"
    if [ -h "$target" ] || [ -d "$target" ] || [ ! -d "${target%/*}" ]; then
      printf 'KEPT\t%s\n' "$rel"
    elif [ ! -e "$target" ]; then
      why=$(mv "$src" "$target" 2>&1) || { cannot_place "$rel" "$why"; return 6; }
    elif cmp -s "$src" "$target"; then
      :
    elif is_prefix "$target" "$src"; then
      why=$(mv -f "$src" "$target" 2>&1) || { cannot_place "$rel" "$why"; return 6; }
    else
      printf 'KEPT\t%s\n' "$rel"
    fi
  done < <(find "$tmp" -type f -print0)

  printf '%s\n' "$(cd "$dest" && pwd -P)"
}

main "$@"; exit $?
