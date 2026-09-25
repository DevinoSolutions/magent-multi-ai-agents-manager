# shellcheck shell=bash
# Include-only (`# @include tmux_floor.sh`): the oldest tmux a node may run
# (DECISION-22). setup.sh refuses anything older and doctor.sh fails it, with
# a predicate the same as or stricter than D's bring_up.sh `need_tmux` (the
# first N.N on the first line of `tmux -V`), so a node that passes setup is
# never one bring_up refuses.
MAGENT_TMUX_MIN_MAJOR=3
MAGENT_TMUX_MIN_MINOR=2

magent_tmux_floor() {
  printf '%s.%s\n' "$MAGENT_TMUX_MIN_MAJOR" "$MAGENT_TMUX_MIN_MINOR"
}

# One line, "<ok|old|unread|missing>\t<first line of tmux -V>". Never fails.
magent_tmux_verdict() {
  local version major minor
  if ! command -v tmux >/dev/null 2>&1; then
    printf 'missing\t\n'
    return 0
  fi
  version=$(tmux -V 2>/dev/null) || version=""
  version=${version%%$'\n'*}
  if ! [[ $version =~ ([0-9]+)\.([0-9]+) ]]; then
    printf 'unread\t%s\n' "$version"
    return 0
  fi
  major=${BASH_REMATCH[1]}
  minor=${BASH_REMATCH[2]}
  if ((10#$major < MAGENT_TMUX_MIN_MAJOR ||
      (10#$major == MAGENT_TMUX_MIN_MAJOR && 10#$minor < MAGENT_TMUX_MIN_MINOR))); then
    printf 'old\t%s\n' "$version"
  else
    printf 'ok\t%s\n' "$version"
  fi
}
