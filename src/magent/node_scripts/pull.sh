#!/usr/bin/env bash
# magent node pull: everything the PC's sync daemon needs from this node, in
# ONE connection. Fed over stdin by remote_mux.pull_node (`bash -s -- <socket>`);
# the payload after the __MAGENT_PAYLOAD__ line is
#   {"sids": {sid: {"roots": [...], "project_dir": name|null, "since": epoch}},
#    "max_member_bytes": N}
# and stdout is, in order (remote_mux.parse_pull's wire format):
#   - the MAGENT-PULL/1 header line;
#   - one JSON metadata line: this node's clock, tmux session list, load
#     sample, each sid's real path, state record names, and `skipped`;
#   - a PLAIN (uncompressed) tar of every file newer than its sid's `since`:
#     <sid>/transcripts/<path under ~/.claude/projects/<project_dir>/> and
#     <sid>/state/<record>.json -- no archive at all when there is none;
#   - the trailer line `MAGENT-PULL-END <member count>`, the LAST bytes,
#     written only after the tar writer closed without error. A failure
#     mid-archive exits non-zero with no trailer: the PC reads it as truncated.
# A file bigger than max_member_bytes is never shipped: it is named under
# `skipped` instead, so the session does not fail. Failing it would freeze its
# watermark forever, since a file that stays too big fails every tick.
# `project_dir` arrives finished (the PC encodes; this script only checks it is
# one name). The tmux socket is lib.sh's $MAGENT_SOCKET: run_script's required
# $1, read and shifted off by lib.sh with no default (DECISION-3,
# DECISION-26 ii); this script takes no args of its own.
# Exits 3 when python3 is missing.
set -euo pipefail
# @include lib.sh

IFS= read -r -d '' MAGENT_PULL_PY <<'MAGENT_PULL_PY' || true
import io
import json
import os
import re
import stat
import sys
import tarfile
import time

now = time.time()
NAME = re.compile(r"[A-Za-z0-9-]+\Z")
home = os.path.expanduser("~")
request = json.loads(sys.stdin.read() or "{}")
if not isinstance(request, dict):
    request = {}
wanted = request.get("sids")
if not isinstance(wanted, dict):
    wanted = {}
cap = request.get("max_member_bytes")
if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
    cap = None  # no cap asked for: the PC's own check still holds


def state_records():
    out = []
    folder = os.path.join(home, ".magent", "state")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        cwd = rec.get("cwd") if isinstance(rec, dict) else None
        if isinstance(cwd, str) and stat.S_ISREG(st.st_mode):
            out.append((name, path, cwd.rstrip("/"), st.st_mtime, st.st_size))
    return out


members = []
skipped = {}
realpaths = {}
state_files = {}
records = state_records()


def offer(sid, arcname, path, mtime, size):
    if cap is not None and size > cap:
        skipped.setdefault(sid, []).append(arcname)
    else:
        members.append((arcname, path, mtime))


for sid, spec in sorted(wanted.items()):
    if not isinstance(spec, dict) or not sid or "/" in sid or sid in (".", ".."):
        continue
    raw_roots = [r for r in spec.get("roots") or [] if isinstance(r, str) and r]
    if not raw_roots:
        continue
    since = spec.get("since")
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        since = 0.0
    roots = []
    for raw in raw_roots:
        expanded = os.path.expanduser(raw).rstrip("/") or "/"
        for candidate in (expanded, os.path.realpath(expanded)):
            if candidate not in roots:
                roots.append(candidate)
    realpaths[sid] = os.path.realpath(os.path.expanduser(raw_roots[0]))
    names = []
    for name, path, cwd, mtime, size in records:
        if any(cwd == r or cwd.startswith(r + "/") for r in roots):
            names.append(name)
            if mtime > since:
                offer(sid, sid + "/state/" + name, path, mtime, size)
    state_files[sid] = names
    pdir = spec.get("project_dir")
    if isinstance(pdir, str) and NAME.match(pdir):
        base = os.path.join(home, ".claude", "projects", pdir)
        for top, dirs, files in os.walk(base):
            dirs.sort()
            for f in sorted(files):
                path = os.path.join(top, f)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_mtime > since:
                    rel = os.path.relpath(path, base).replace(os.sep, "/")
                    offer(sid, sid + "/transcripts/" + rel, path, st.st_mtime, st.st_size)

sample = None
try:
    sample = json.loads(os.environ.get("MAGENT_PULL_SAMPLE", ""))
except ValueError:
    pass
meta = {
    "now": now,
    "sessions": [s for s in os.environ.get("MAGENT_PULL_SESSIONS", "").splitlines() if s],
    "sample": sample,
    "realpaths": realpaths,
    "state_files": state_files,
    "skipped": {sid: sorted(names) for sid, names in skipped.items()},
}
out = sys.stdout.buffer
out.write(b"MAGENT-PULL/1\n")
out.write(json.dumps(meta).encode("utf-8") + b"\n")
out.flush()
count = 0
if members:
    # "w|": a plain stream, never compressed -- the PC refuses anything else.
    with tarfile.open(fileobj=out, mode="w|") as tar:
        for arcname, path, mtime in members:
            try:
                with open(path, "rb") as fh:
                    # Bounded: a file that grew past the cap since the scan
                    # ships its first `cap` bytes, like any file growing
                    # mid-pull; the next tick names it under `skipped`.
                    data = fh.read() if cap is None else fh.read(cap)
            except OSError:
                continue
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mtime = int(mtime)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
            count += 1
# Only reached when the archive closed cleanly: an exception above exits 1
# with no trailer, and the PC reads the reply as truncated.
out.write(b"MAGENT-PULL-END %d\n" % count)
out.flush()
MAGENT_PULL_PY

main() {
  local sessions sample
  if ! command -v python3 >/dev/null 2>&1; then
    echo "pull.sh: python3 is required on the node" >&2
    exit 3
  fi
  # Neither may read stdin: the rest of it is the payload.
  sessions=$(tmux -L "$MAGENT_SOCKET" list-sessions -F '#{session_name}' </dev/null 2>/dev/null || true)
  sample=$(magent_sample </dev/null 2>/dev/null || true)
  magent_payload | MAGENT_PULL_SESSIONS="$sessions" MAGENT_PULL_SAMPLE="$sample" python3 -c "$MAGENT_PULL_PY"
}

main "$@"; exit $?
