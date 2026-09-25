#!/usr/bin/env bash
# magent node pull: everything the PC's sync daemon needs from this node, in
# ONE connection. Fed over stdin by remote_mux.pull_node (`bash -s -- <socket>`);
# the payload after the __MAGENT_PAYLOAD__ line is
#   {"sids": {sid: {"roots": [...], "project_dir": name|null, "since": epoch}},
#    "max_member_bytes": N, "max_total_bytes": N}
# and stdout is, in order (remote_mux.parse_pull's wire format):
#   - the MAGENT-PULL/1 header line;
#   - one JSON metadata line: this node's clock, tmux session list, load
#     sample, each sid's real path, state record names, `skipped`,
#     `unreadable` and `truncated` (each {sid: [archive name, ...]}), and
#     `resume` ({sid: mtime});
#   - a PLAIN (uncompressed) tar of every file newer than its sid's `since`:
#     <sid>/transcripts/<path under ~/.claude/projects/<project_dir>/> and
#     <sid>/state/<record>.json -- no archive at all when there is none;
#   - the trailer line `MAGENT-PULL-END <member count>`, the LAST bytes,
#     written only after the tar writer closed without error. A failure
#     mid-archive exits non-zero with no trailer: the PC reads it as truncated.
# A file bigger than max_member_bytes is never shipped: it is named under
# `skipped` instead, so the session does not fail. Failing it would freeze its
# watermark forever, since a file that stays too big fails every tick. A file
# this user cannot read (any errno but ENOENT) is named under `unreadable`
# for the same reason. Only regular files are ever opened, never through a
# symlink, and a symlinked project dir is not followed.
# The whole reply stays within max_total_bytes (the PC kills a bigger one, so
# a backlog past it would fail every tick). Files go oldest first; the first
# that would not fit and every file after it are named under `truncated`, and
# `resume` gives each such sid the mtime of its oldest file left out, so the
# PC's next `since` continues from there.
# `project_dir` arrives finished (the PC encodes; this script only checks it is
# one name). The tmux socket is lib.sh's $MAGENT_SOCKET: run_script's required
# $1, read and shifted off by lib.sh with no default (DECISION-3,
# DECISION-26 ii); this script takes no args of its own.
# Exits 3 when python3 is missing.
set -euo pipefail
# @include lib.sh

IFS= read -r -d '' MAGENT_PULL_PY <<'MAGENT_PULL_PY' || true
import errno
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
# A state record is a few hundred bytes; anything bigger is not one.
STATE_MAX_BYTES = 64 * 1024
home = os.path.expanduser("~")
request = json.loads(sys.stdin.read() or "{}")
if not isinstance(request, dict):
    request = {}
wanted = request.get("sids")
if not isinstance(wanted, dict):
    wanted = {}


def byte_cap(value):
    # None when no cap was asked for: the PC's own checks still hold.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


cap = byte_cap(request.get("max_member_bytes"))
total_cap = byte_cap(request.get("max_total_bytes"))
HEADER = b"MAGENT-PULL/1\n"
BLOCK = 512
# Room for the trailer line, and for what tar's close writes: two zero blocks,
# then padding to its 10240-byte record.
TAIL_ROOM = 64 + 2 * BLOCK + tarfile.RECORDSIZE


def open_regular(path):
    """A readable file object for `path` only while it IS a regular file:
    O_NOFOLLOW refuses a symlink (a record aimed at /dev/zero), O_NONBLOCK
    keeps a FIFO from blocking the open, and fstat on the open fd -- not the
    scan's lstat -- decides, so a file swapped after the scan cannot get
    through. None when it is not a regular file; OSError as open raises it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


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
        # Only a small regular file is ever opened: a FIFO named x.json would
        # block the read forever, and a symlink or a huge file is unbounded.
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > STATE_MAX_BYTES:
            continue
        try:
            fh = open_regular(path)
            if fh is None:
                continue
            with fh:
                rec = json.loads(fh.read(STATE_MAX_BYTES + 1).decode("utf-8"))
        except (OSError, ValueError):
            continue
        cwd = rec.get("cwd") if isinstance(rec, dict) else None
        if isinstance(cwd, str):
            out.append((name, path, cwd.rstrip("/"), st.st_mtime, st.st_size))
    return out


members = []
skipped = {}
unreadable = {}
realpaths = {}
state_files = {}
records = state_records()


def offer(sid, arcname, path, mtime, size):
    if cap is not None and size > cap:
        skipped.setdefault(sid, []).append(arcname)
    else:
        members.append((mtime, arcname, sid, path, size))


def cannot_read(sid, arcname, error):
    """A file that vanished (ENOENT) is simply gone. Any other errno (EACCES,
    EIO) is named under `unreadable`, never dropped in silence: the watermark
    moves past it either way, and the PC is the one to report it."""
    if error.errno != errno.ENOENT:
        unreadable.setdefault(sid, []).append(arcname)


def transcripts(sid, base, since):
    # A symlinked project dir is never followed: it could point anywhere,
    # ~/.ssh included. os.walk itself never descends a symlinked subdir.
    if os.path.islink(base):
        return

    def walk_error(error):
        rel = os.path.relpath(error.filename or base, base).replace(os.sep, "/")
        cannot_read(sid, sid + "/transcripts" + ("" if rel == "." else "/" + rel), error)

    for top, dirs, files in os.walk(base, onerror=walk_error):
        dirs.sort()
        for f in sorted(files):
            path = os.path.join(top, f)
            arcname = sid + "/transcripts/" + os.path.relpath(path, base).replace(os.sep, "/")
            try:
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode) or st.st_mtime <= since:
                    continue
                # Opened once here so a file this user cannot read is named
                # in the metadata, which is written before the archive.
                fh = open_regular(path)
            except OSError as e:
                cannot_read(sid, arcname, e)
                continue
            if fh is None:
                continue
            fh.close()
            offer(sid, arcname, path, st.st_mtime, st.st_size)


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
        transcripts(sid, os.path.join(home, ".claude", "projects", pdir), since)

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
    "unreadable": {sid: sorted(names) for sid, names in unreadable.items()},
}


def padded(n):
    return -(-n // BLOCK) * BLOCK


def member_cost(arcname, size):
    # Its header, its bytes padded to a block, and room for the PAX header
    # tar adds for a long or non-ASCII name. Never less than tar writes.
    # The name's bytes as tar writes them: os.walk surrogate-escapes a name
    # that is not UTF-8, and a strict encode here would raise and fail the
    # whole node's pull. The PAX records' worst case is N + 62 bytes for an
    # N-byte name: path (N + 12), hdrcharset=BINARY for such a name (21), and
    # an mtime past ustar's range (up to 29); 128 covers it.
    name = arcname.encode("utf-8", "surrogateescape")
    return BLOCK + padded(size) + BLOCK + padded(len(name) + 128)


def listed_cost(text):
    # One JSON string in a list or as a key, with its separator.
    return len(json.dumps(text)) + 2


# Oldest first, so what fits is always everything older than what did not:
# `resume` is then a watermark that loses nothing.
members.sort()
shipped = members
truncated = {}
resume = {}
if total_cap is not None:
    # The metadata is written before the archive, so what it may still grow
    # by -- every member named under `truncated`, every sid given a `resume`
    # -- is reserved up front, as if nothing fit. A listing too long for even
    # that is a node with millions of files; its reply is refused on the PC.
    reserve = sum(listed_cost(m[1]) for m in members) + sum(
        2 * listed_cost(sid) + 32 for sid in {m[2] for m in members}
    )
    free = total_cap - len(HEADER) - len(json.dumps(meta)) - 64 - reserve - TAIL_ROOM
    shipped = []
    for member in members:
        mtime, arcname, sid, path, size = member
        cost = member_cost(arcname, size)
        if truncated or cost > free:
            truncated.setdefault(sid, []).append(arcname)
            resume.setdefault(sid, mtime)
            continue
        free -= cost
        shipped.append(member)
meta["truncated"] = {sid: sorted(names) for sid, names in truncated.items()}
meta["resume"] = resume
out = sys.stdout.buffer
out.write(HEADER)
out.write(json.dumps(meta).encode("utf-8") + b"\n")
out.flush()
count = 0
if shipped:
    # "w|": a plain stream, never compressed -- the PC refuses anything else.
    with tarfile.open(fileobj=out, mode="w|") as tar:
        for mtime, arcname, sid, path, size in shipped:
            # Opened again the same safe way: the scan's check is old news.
            # Gone since the scan (ENOENT), or no longer a regular file (a
            # symlink swapped in answers ELOOP): nothing left to ship. Any
            # other errno cannot be named any more -- the metadata is already
            # written -- so it propagates: exit 1, no trailer, and the PC
            # reads a truncated reply and moves no watermark.
            try:
                fh = open_regular(path)
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ELOOP):
                    continue
                raise
            if fh is None:
                continue
            with fh:
                # Bounded by the size the scan saw, which the member and
                # total caps were checked against: a file that grew since
                # ships that much, and its new mtime (after `now`) asks for
                # the rest next tick.
                data = fh.read(size)
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
