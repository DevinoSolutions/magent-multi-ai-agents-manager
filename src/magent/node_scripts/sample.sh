#!/usr/bin/env bash
# magent node sample: one JSON line describing this node's load right now.
# Fed over stdin by remote_mux.run_script (`bash -s -- <socket>`); no payload.
set -euo pipefail
# @include lib.sh

main() {
  magent_sample
}

main "$@"; exit $?
