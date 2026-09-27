# magent node library, inlined into EVERY node script by that script's
# include line for lib.sh (right after its `set` line; see node_scripts'
# docstring). Defines functions and takes the socket argument; never calls
# main. This header must not spell the include marker itself: the loader
# does not recurse, so a marker left in lib.sh would survive into every script.

# The calling convention (remote_mux.run_script): $1 is ALWAYS the tmux socket
# name, remote_mux.SOCKET -- its one owner (DECISION-3). No default: a script
# run without it fails loudly rather than guess a server. Shifted off, so
# the including script's main sees only the caller's own arguments.
MAGENT_SOCKET="${1:?magent: the tmux socket name is a required first argument}"
shift

# Skip stdin up to the __MAGENT_PAYLOAD__ line (remote_mux.PAYLOAD_SENTINEL),
# then copy the payload to stdout. Call it at most once per script, and BEFORE
# any command that may read stdin -- the payload is the rest of the script's
# own stdin, and a background `cat` started first was shown to eat it -- or
# give such a child `</dev/null`. No sentinel line (or one without its
# newline) returns 1, loudly: a MISSING payload is not an empty one, and under
# `set -e` the script dies instead of carrying on with nothing.
magent_payload() {
  local line found=0
  while IFS= read -r line; do
    if [ "$line" = __MAGENT_PAYLOAD__ ]; then
      found=1
      break
    fi
  done
  if [ "$found" -ne 1 ]; then
    echo "magent: no __MAGENT_PAYLOAD__ line on stdin" >&2
    return 1
  fi
  cat
}

# One JSON line describing this node's load right now (Linux /proc -- the
# node pool is Linux). Field names are nodes.LoadSample's.
magent_sample() {
  local nproc load1 load5 load15 _rest mem_total_kb mem_avail_kb sessions ts
  nproc=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)
  read -r load1 load5 load15 _rest < /proc/loadavg
  mem_total_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  mem_avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  # bash reads an empty variable as 0 in $(( )): without this a kernel that
  # lacks the field would report a node with no memory free, not a failure.
  if [ -z "$mem_avail_kb" ]; then
    echo "magent: MemAvailable missing from /proc/meminfo" >&2
    return 1
  fi
  # No tmux server yet (or no tmux at all) is zero sessions, not a failure.
  sessions=$(tmux -L "$MAGENT_SOCKET" list-sessions 2>/dev/null | wc -l | tr -d ' ' || true)
  ts=$(date +%s)
  printf '{"ts": %s, "nproc": %s, "load1": %s, "load5": %s, "load15": %s, "mem_total_mb": %s, "mem_avail_mb": %s, "my_sessions": %s}\n' \
    "$ts" "$nproc" "$load1" "$load5" "$load15" \
    "$((mem_total_kb / 1024))" "$((mem_avail_kb / 1024))" "${sessions:-0}"
}
