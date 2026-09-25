#!/usr/bin/env bash
# install_transcripts.sh <encoded name> -- stdin carries this script, the line
# __MAGENT_PAYLOAD__, then a tar of a Claude project directory's CONTENTS
# (<uuid>.jsonl, <uuid>/subagents/, memory/). They are extracted into
# ~/.claude/projects/<encoded name>/, which is printed. Files already there are
# overwritten; nothing is ever deleted.
#
# The name was computed by magent's one encoder (nodes.encoded_project_dir);
# this script only refuses anything outside that encoder's alphabet, so a bad
# argument can never write outside ~/.claude/projects. The tmux socket arrives
# as $1 and lib.sh shifts it off (DECISION-26 ii); the payload is read with
# lib.sh's magent_payload.
set -euo pipefail
# @include lib.sh

main() {
  local name="${1:-}" dest
  case "$name" in
    "" | *[!A-Za-z0-9-]*)
      printf 'install_transcripts.sh: not an encoded project dir name: %s\n' "$name" >&2
      return 2
      ;;
  esac
  dest="$HOME/.claude/projects/$name"
  mkdir -p "$dest"
  magent_payload | tar -xf - -C "$dest"
  printf '%s\n' "$(cd "$dest" && pwd -P)"
}

main "$@"; exit $?
