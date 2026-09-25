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
# Nothing is ever deleted. Everything created is private (umask 077; tar never
# restores the payload's owner or modes).
#
# Exit codes: 2 the name is outside the encoder's alphabet; 3 the payload is
# missing or broken; 4 the destination (or a directory inside it the payload
# needs) is a symlink, which could point anywhere.
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
# already been reported KEPT, and mkdir -p beneath it would fail.
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

main() {
  local name="${1:-}" projects dest src rel target
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
  mkdir -p "$projects"
  tmp="$(mktemp -d "$projects/.magent-install.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  if ! magent_payload | tar --no-same-owner --no-same-permissions -xf - -C "$tmp"; then
    printf 'install_transcripts.sh: the payload is missing or broken; nothing installed\n' >&2
    return 3
  fi

  # Every directory the payload needs, checked before anything is placed.
  while IFS= read -r -d '' src; do
    if [ -h "$dest/${src#"$tmp"/}" ]; then
      printf 'install_transcripts.sh: %s is a symlink; not installing through it\n' \
        "$dest/${src#"$tmp"/}" >&2
      return 4
    fi
  done < <(find "$tmp" -mindepth 1 -type d -print0)

  mkdir -p "$dest"
  while IFS= read -r -d '' src; do
    rel="${src#"$tmp"/}"
    if under_a_file "$dest" "$rel"; then
      :  # its ancestor was KEPT already; the files under it are KEPT below
    elif [ -e "$dest/$rel" ] && [ ! -d "$dest/$rel" ]; then
      printf 'KEPT\t%s\n' "$rel"  # a file where the payload has a directory
    else
      mkdir -p "$dest/$rel"
    fi
  done < <(find "$tmp" -mindepth 1 -type d -print0)

  while IFS= read -r -d '' src; do
    rel="${src#"$tmp"/}"
    target="$dest/$rel"
    if [ -h "$target" ] || [ -d "$target" ] || [ ! -d "${target%/*}" ]; then
      printf 'KEPT\t%s\n' "$rel"
    elif [ ! -e "$target" ]; then
      mv "$src" "$target"
    elif cmp -s "$src" "$target"; then
      :
    elif is_prefix "$target" "$src"; then
      mv -f "$src" "$target"
    else
      printf 'KEPT\t%s\n' "$rel"
    fi
  done < <(find "$tmp" -type f -print0)

  printf '%s\n' "$(cd "$dest" && pwd -P)"
}

main "$@"; exit $?
