#!/usr/bin/env bash
# magent node bring-up: a placeholder until nodes plan D Task 8 replaces it.
# Fed over stdin by remote_mux.run_script (`bash -s -- <socket> <mode> ...`).
set -euo pipefail
# @include lib.sh

main() { echo "bring_up.sh: replaced in Task 8" >&2; return 2; }

main "$@"; exit $?
