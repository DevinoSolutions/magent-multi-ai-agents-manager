#!/bin/sh
# `claude` on the nodes e2e tier's loopback node (tests/e2e/test_nodes_real.py).
# The nodes-e2e workflow installs it as /usr/local/bin/claude on the runner,
# which is the node, so a node user's login PATH finds it exactly where a
# real install would put an agent. It is the one substitution on the node's
# side of the wire: everything magent runs there -- bash, tmux, git, ssh --
# is real.
#
# `--version` and `auth status --json` answer like Claude Code (logged out),
# for the setup and doctor rows. Anything else runs the stand-in agent that
# the test repo commits beside its code; the pane's cwd is the project folder.
case "${1:-}" in
  --version)
    echo "2.1.0 (Claude Code)"
    exit 0
    ;;
  auth)
    if [ "${2:-}" = status ]; then
      echo '{"loggedIn": false}'
      exit 1
    fi
    ;;
esac
agent="$PWD/.magent-e2e/node_agent.py"
if [ ! -f "$agent" ]; then
  echo "claude (e2e stand-in): $agent not found; this stub only runs the test repo's agent" >&2
  exit 127
fi
exec python3 "$agent" "$@"
