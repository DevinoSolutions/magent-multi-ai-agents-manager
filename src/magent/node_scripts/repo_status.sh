#!/usr/bin/env bash
# repo_status.sh <remote_root> -- read-only report of a node session's repos,
# one tab-separated line each:
#   <dir>  <head sha>  <branch>  <dirty: true|false|unknown>  <unpushed count, -1 unknown>
# <remote_root> is the session's cwd and may start with "~/". It is a repo
# itself, or a workspace whose immediate children are repos (spec §7b). With
# no repo at all it prints "<remote_root>\t\t\tmissing\t-1". Never fetches,
# never writes: recall reports what is on the node, it does not change it.
# run_script passes the tmux socket as $1 and lib.sh shifts it off
# (DECISION-26 ii); this script never touches tmux. A git read that fails is
# reported as unknown, never fatal: every one is guarded with `|| ...`.
# A `git status` that fails (a corrupt index, a repo git refuses to trust) is
# `unknown`, never `false`: a clean report is a claim the work is safe.
# Status runs with --no-optional-locks, so reading it never rewrites the index.
# Known limit: a child directory whose NAME holds a TAB or newline breaks the
# TAB-separated rows; the parser drops a row that is not exactly five fields.
# Hidden children (.name) are never workspace repos: the glob skips them.
set -euo pipefail
# @include lib.sh

report() {
  local dir="$1" abs="$2" head branch dirty unpushed
  head="$(git -C "$abs" rev-parse HEAD 2>/dev/null)" || head=""
  branch="$(git -C "$abs" rev-parse --abbrev-ref HEAD 2>/dev/null)" || branch=""
  local porcelain
  if porcelain="$(git --no-optional-locks -C "$abs" status --porcelain 2>/dev/null)"; then
    if [ -n "$porcelain" ]; then dirty=true; else dirty=false; fi
  else
    dirty=unknown
  fi
  unpushed="$(git -C "$abs" rev-list --count '@{upstream}..HEAD' 2>/dev/null)" || unpushed=-1
  printf '%s\t%s\t%s\t%s\t%s\n' "$dir" "$head" "$branch" "$dirty" "$unpushed"
}

main() {
  local root="${1:?usage: repo_status.sh <remote_root>}" abs child found=0
  abs="${root/#\~/$HOME}"
  if [ -e "$abs/.git" ]; then
    report "$root" "$abs"
    return 0
  fi
  for child in "$abs"/*/; do
    [ -e "${child}.git" ] || continue
    child="${child%/}"
    report "$root/${child##*/}" "$child"
    found=1
  done
  [ "$found" = 1 ] || printf '%s\t\t\tmissing\t-1\n' "$root"
}

main "$@"; exit $?
