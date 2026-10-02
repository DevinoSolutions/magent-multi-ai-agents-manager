"""Tiny upload server for mobile image transfer to psmux sessions."""

from __future__ import annotations

import contextlib
import errno
import html
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Sequence

from magent import pidfile, psmux, tailnet
from magent.icons import render_icon
from magent.lockfile import LockHeld, exclusive_lock
from magent.log import get_logger, log_safe
from magent.sessions import (
    FLASH_MSG_MAX,
    FLASH_TINT_ERR,
    FLASH_TINT_OK,
    paths_line,
    upload_limit_text,
)
from magent.sessions import MAX_UPLOAD_BYTES as _SHARED_MAX_UPLOAD_BYTES


def _pid_path(port: int) -> Path:
    return Path.home() / ".magent" / f"upload_server-{port}.pid"


def server_pid(port: int) -> int | None:
    """Return the PID of the upload server recorded for this port, if it is
    running.

    A record of a process that is gone, or that predates the last boot, names
    no server of ours (the OS hands pid numbers out again) and is cleared -- see
    ``pidfile.read``. Every reader acts on the number: `status` (DEAD vs off),
    `stop_server` (what to end) and the phone-URL port pick. The attention
    watchdog reads it for its log line only and decides on the port probe alone.
    """
    return pidfile.read(_pid_path(port))


def stop_server(port: int) -> bool:
    """Stop the upload server running on the given port. Returns True only if
    the process was actually ended. On failure the pid file is kept (not
    unlinked) so `status` or a retry can still find the process; a pid file
    whose number now names a different process is cleared and ends nothing."""
    log = get_logger("upload")
    path = _pid_path(port)
    pid, outcome = pidfile.terminate(path)
    if outcome == "terminated":
        pidfile.clear_stale(path)
        return True
    if outcome == "mismatch":
        log.warning(
            "pid %s is not the upload server that wrote %s; left alone", pid, path.name
        )
        pidfile.clear_stale(path)
    elif outcome in ("failed", "unverifiable"):
        log.warning("could not stop upload server pid %s (%s)", pid, outcome)
    return False


_UPLOAD_DIR = Path.home() / ".magent" / "uploads"

# Memory-exhaustion guard: reject a declared/actual body past this size
# instead of reading it all into memory. Not an auth control -- just an
# operability ceiling on the hot path. Per REQUEST, so an Alt+V press carrying
# several files shares one budget. The number lives in `magent.sessions`
# because the Alt+V listener pre-checks it before reading a file off disk.
MAX_UPLOAD_BYTES = _SHARED_MAX_UPLOAD_BYTES

# The multipart framing a request carries on top of its files: boundaries,
# each part's headers, the project/inject fields -- a few hundred bytes a file.
# The limit a user is told is "100 MB of files", and that is what the page and
# the Alt+V listener pre-check (summed file sizes); the REQUEST may be this much
# larger, so a selection the pre-check passes is never refused for its
# envelope. The files are held to MAX_UPLOAD_BYTES once parsed. Fixed and
# named, so the ceiling on what is read into memory stays a known number.
MULTIPART_ALLOWANCE_BYTES = 1024 * 1024


def _too_large() -> dict[str, object]:
    """The 413 envelope. It names the FILES limit a user can act on, whichever
    check refused the request."""
    return {
        "ok": False,
        "error": f"File too large - {upload_limit_text(MAX_UPLOAD_BYTES)} limit",
    }


def _request_limit() -> int:
    """Largest Content-Length read into memory. At call time, so a test that
    lowers MAX_UPLOAD_BYTES lowers this with it."""
    return MAX_UPLOAD_BYTES + MULTIPART_ALLOWANCE_BYTES


# --- Rejected-request drain (P4-02) -----------------------------------------
# Windows failure mode this guards: when the handler sends an early 4xx and
# closes while unread request-body bytes still sit in the socket's receive
# buffer, the OS emits a TCP RST -- so the client (the phone upload page) sees a
# connection reset instead of our JSON error envelope, and the reject tests
# flake for the same reason. Every reject-before-read path therefore drains the
# pending body first, then closes the connection.
#
# The drain is bounded so a lying, garbage, or endless Content-Length can never
# make the handler read forever: at most _DRAIN_CAP_BYTES are discarded, in
# _DRAIN_CHUNK_BYTES blocks, with a short per-read timeout that stops us waiting
# on a client which declared more than it actually sent. The cap mirrors the
# upload ceiling but is its OWN constant -- tuning MAX_UPLOAD_BYTES (or a test
# lowering it to force a 413) must never quietly unbound the drain.
#
# It sits PAST the request ceiling, by `_DRAIN_SLACK_BYTES`, because the client
# the drain exists for is the honest one that sent a body just over the limit:
# a drain that stops reading that body partway closes on unread bytes, and the
# RST it was built to prevent comes back -- exactly for the over-limit reply
# that most needs to arrive. A body more than the slack over the ceiling is cut
# off and the connection closed; that client may see a reset, which is what a
# megabyte-plus overshoot of a published limit earns. Reading ~102 MB into a
# discard buffer costs time on a rejected request, never memory: it is read in
# `_DRAIN_CHUNK_BYTES` blocks and each block is dropped.
_DRAIN_SLACK_BYTES = 1024 * 1024
_DRAIN_CAP_BYTES = MAX_UPLOAD_BYTES + MULTIPART_ALLOWANCE_BYTES + _DRAIN_SLACK_BYTES
_DRAIN_CHUNK_BYTES = 64 * 1024
_DRAIN_TIMEOUT_S = 0.5

# How long one read or write on a client connection may wait for progress
# (see UploadHandler.timeout). A minute of total silence mid-upload is a dead
# link on any network magent is reached over, loopback or tailnet.
CONNECTION_TIMEOUT_S = 60.0

# In-session upload feedback for the MOBILE page: a paste's progress shows in
# the SAME magent:<project> window it landed in, via the psmux (tmux) status
# line -- never drawn into the agent pane. tmux 3.3 renders these UTF-8 glyphs
# intact. An Alt+V paste narrates itself instead (see altv.handle_press and the
# `flagged` note in _handle_post): one bar, one voice.
_FB_OK = "✓"  # check mark -- uploaded
_FB_NO = "✗"  # ballot x   -- failed

# Color the flash so state reads at a glance: green while uploading AND on
# success, red on failure. This Cygwin tmux 3.3.6 does NOT expand inline #[...]
# style directives inside display-message (it prints them verbatim), so we tint
# the whole message bar via message-style instead -- set just before the flash,
# scoped to that project's own socket. It's overwritten on the next flash and
# only styles the transient message line, never the agent pane.
_MSG_GREEN = "bg=green,fg=black,bold"
_MSG_RED = "bg=red,fg=white,bold"

# What a caller-supplied ``tint=`` maps to. An unknown value leaves the style
# alone rather than failing the flash -- the message matters more than its
# colour.
_FLASH_TINTS = {FLASH_TINT_OK: _MSG_GREEN, FLASH_TINT_ERR: _MSG_RED}

# How long each status-line flash lingers (ms).
_FLASH_OK_MS = 2500
_FLASH_NO_MS = 3000

# /api/flash: how long a caller-supplied message lingers by default. Long enough
# to read a whole sentence, short enough that a stale one clears itself. The
# Alt+V/F2 listener is the only caller today -- it runs hidden with no terminal
# of its own, so this endpoint is its ONLY way to say anything on screen.
_FLASH_MSG_MS = 4000

# ...and the bounds on what a caller may ask for instead. A PHASE message
# ("uploading...") must outlive the operation it describes or the bar goes
# blank mid-press; the ceiling keeps a bad or hostile value from parking a
# message on the bar for the rest of the day.
_FLASH_MSG_MS_MIN = 500
_FLASH_MSG_MS_MAX = 60000

# Guards UploadHandler.cached_sessions / sessions_ts: UploadHandler is
# instantiated per-request by ThreadingHTTPServer, so refresh must be
# single-flight or concurrent requests can race the read-check-write.
_sessions_lock = threading.Lock()


def _flash_duration(raw: str) -> int:
    """Clamp a caller-supplied ``ms=`` to the allowed window. Anything absent or
    unparseable takes the default rather than failing the flash: a malformed
    duration must never be the reason a message does not reach the screen."""
    try:
        wanted = int(raw)
    except (TypeError, ValueError):
        return _FLASH_MSG_MS
    return max(_FLASH_MSG_MS_MIN, min(_FLASH_MSG_MS_MAX, wanted))


def _flash(
    _psmux_unused: str | None,
    project: str,
    message: str,
    duration_ms: int,
    style: str | None = None,
) -> None:
    """Best-effort status-line flash. Delegates to ``psmux.flash_message``."""
    psmux.flash_message(project, message, duration_ms, style=style)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>magent upload</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#1e1e2e">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="magent upload">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" href="/icon-192.png">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:#1e1e2e;color:#cdd6f4;
  padding:12px;-webkit-tap-highlight-color:transparent}
.head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.head h1{font-size:.85rem;color:#a6e3a1;font-weight:700;letter-spacing:.5px}
.head span{color:#45475a;font-size:.7rem}
.pills{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.pill{background:#313244;border:1.5px solid #45475a;border-radius:20px;
  padding:6px 14px;font-size:.8rem;color:#bac2de;cursor:pointer;
  transition:all .12s;white-space:nowrap;-webkit-user-select:none;user-select:none}
.pill:active{transform:scale(.96)}
.pill.on{border-color:#89b4fa;background:#1e3a5f;color:#89b4fa;font-weight:600}
/* Filtered out by the typeahead. Hidden, never removed: the pill keeps its
   listeners and its config position, so clearing the query restores the list
   exactly as the config wrote it. */
.pill.off{display:none}
/* The keyboard highlight. A ring rather than a recolour, so it composes with
   `.on` instead of fighting it -- the highlighted pill and the SELECTED pill
   are different questions and a phone user needs both answers at once. */
.pill.hi{box-shadow:0 0 0 2px #f9e2af}
#proj-filter{width:100%;padding:9px 12px;margin-bottom:8px;font-family:inherit;
  font-size:.85rem;color:#cdd6f4;background:#181825;border:1.5px solid #45475a;
  border-radius:8px}
#proj-filter:focus{outline:none;border-color:#89b4fa}
#proj-filter::placeholder{color:#585b70}
.nomatch{display:none;color:#f9e2af;font-size:.78rem;padding:2px 0 6px}
.nomatch.show{display:block}
/* The selected project, always on screen -- the filter can hide the pill that
   carries the `.on` state, and "which session am I about to send to" must not
   be a question the query can erase. */
.chosen{font-size:.75rem;color:#585b70;margin-bottom:10px}
.chosen.on{color:#89b4fa;font-weight:600}
.drop{border:1.5px dashed #45475a;border-radius:10px;padding:18px 12px;
  text-align:center;color:#585b70;font-size:.8rem;position:relative;
  transition:all .15s;margin-bottom:8px}
.drop.ready{border-color:#89b4fa;color:#89b4fa;border-style:solid}
.drop.busy{border-color:#f9e2af;color:#f9e2af}
.drop.ok{border-color:#a6e3a1;color:#a6e3a1}
/* Paste still pending: the healthy tint, but unfinished. Green says the file
   is safe (it is, on disk); the dashed edge says psmux has not answered yet. */
.drop.pend{border-style:dashed}
.drop.err{border-color:#f38ba8;color:#f38ba8}
.drop input{position:absolute;inset:0;opacity:0;cursor:pointer;font-size:0}
.paste{display:none;margin-bottom:8px;border:1.5px solid #45475a;border-radius:10px;
  padding:10px;background:#181825}
.paste.show{display:block}
.paste img{display:block;max-width:100%;max-height:40vh;border-radius:6px;
  margin:0 auto 8px;background:#11111b}
/* A pasted file that is not an image: its name on a tile, where an image would
   show its preview. */
.paste-file{display:none;margin:0 auto 8px;padding:18px 12px;border-radius:6px;
  background:#11111b;color:#cdd6f4;font-size:.85rem;text-align:center;
  word-break:break-all}
.paste-file.show{display:block}
.paste-meta{display:flex;justify-content:space-between;gap:8px;font-size:.75rem;
  color:#9399b2;margin-bottom:8px}
#paste-dest{color:#89b4fa;font-weight:600}
.bar{display:none;height:6px;border-radius:3px;background:#313244;overflow:hidden;
  margin-bottom:8px}
.bar.show{display:block}
#bar-fill{height:100%;width:0%;background:#89b4fa;transition:width .15s}
.paste-actions{display:flex;gap:8px}
.paste-actions button{flex:1;padding:9px;border:none;border-radius:8px;
  font-weight:700;font-size:.8rem;cursor:pointer}
#paste-send{background:#a6e3a1;color:#1e1e2e}
#paste-send:disabled{background:#45475a;color:#6c7086;cursor:not-allowed}
#paste-send.ok{background:#a6e3a1}
#paste-send.err{background:#f38ba8;color:#1e1e2e}
#paste-cancel{background:#313244;color:#bac2de}
.toast{font-size:.75rem;color:#6c7086;text-align:center;min-height:1.1em;
  transition:color .2s}
.toast.ok{color:#a6e3a1}
.toast.err{color:#f38ba8}
.none{color:#f38ba8;font-size:.8rem;padding:12px 0}
.install{display:none;margin-top:14px;padding:10px 12px;border:1px solid #313244;
  border-radius:10px;background:#181825;font-size:.72rem;color:#9399b2;line-height:1.5}
.install.show{display:block}
.install b{color:#a6e3a1;font-weight:600}
.install button{margin-top:8px;width:100%;padding:8px;border:none;border-radius:8px;
  background:#a6e3a1;color:#1e1e2e;font-weight:700;font-size:.78rem;cursor:pointer}
.install .x{float:right;color:#585b70;cursor:pointer;font-size:.9rem;line-height:1}
</style>
</head>
<body>
<div class="head">
  <h1>magent</h1>
  <span>tap or type a project &rsaquo; tap file or Ctrl+V &rsaquo; done</span>
</div>

<input type="text" id="proj-filter" placeholder="type to filter projects"
  autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
  enterkeyhint="go" aria-label="filter projects">
<div class="pills" id="pills">PROJECTS_PLACEHOLDER</div>
<p class="nomatch" id="proj-nomatch">no project matches</p>
<div class="chosen" id="proj-chosen">no project selected</div>

<div class="drop" id="drop">
  <span id="drop-label">select a project first</span>
  <input type="file" id="file" multiple disabled>
</div>

<div class="paste" id="paste-box">
  <img id="paste-img" alt="pasted image">
  <div class="paste-file" id="paste-file"><span id="paste-name"></span></div>
  <div class="paste-meta">
    <span id="paste-dest"></span>
    <span id="paste-size"></span>
  </div>
  <div class="bar" id="paste-bar"><div id="bar-fill"></div></div>
  <div class="paste-actions">
    <button id="paste-send" disabled>Send</button>
    <button id="paste-cancel">Cancel</button>
  </div>
</div>
<div class="toast" id="toast">&nbsp;</div>

<div class="install" id="install">
  <span class="x" id="install-x">&times;</span>
  <span id="install-text"></span>
  <button id="install-btn" style="display:none">Install app</button>
</div>

<script>
let proj = null;
const pills = document.querySelectorAll('.pill');
const drop = document.getElementById('drop');
const label = document.getElementById('drop-label');
const input = document.getElementById('file');
const toast = document.getElementById('toast');

// The upload reply carries THREE paste states, not two (DESIGN.md "The upload
// reply is not hostage to the paste"):
//
//   injected:true                    -> it is in the agent's pane;
//   inject_pending:true              -> the FILE IS ON DISK and psmux has not
//                                       answered yet -- the reply is early,
//                                       not wrong;
//   neither                          -> no paste happened (psmux refused it,
//                                       or there is no psmux at all).
//
// Only `ok:false` is a failed upload. Reading `injected` alone, as this page
// used to, collapses the middle state into the last one and shows a
// failure-looking result about a screenshot that is safely stored and about to
// paste -- exactly the lie the status line was fixed for. The pending wording
// mirrors altv.OUTCOME_REASONS['inject-pending'] (a unit test pins the two
// together) so the phone and the status bar say the same thing, and it keeps
// the healthy tint for the same reason the bar does: red reads as "your
// screenshot is gone".
const PEND_LABEL = 'saved - paste still pending';
const PEND_NOTE = ' saved - psmux is slow, paste still pending';
function isPending(d) { return !!(d && !d.injected && d.inject_pending); }

// Any file type goes, up to the server's own limit -- checked HERE before a
// byte is sent, so a phone does not spend a minute uploading a file the
// server will only refuse. Both numbers are filled in from
// sessions.MAX_UPLOAD_BYTES when the page is served, so they cannot drift.
const MAX_BYTES = MAX_UPLOAD_BYTES_PLACEHOLDER;
const MAX_LABEL = 'MAX_UPLOAD_LABEL_PLACEHOLDER';
const TOO_BIG = 'too large - ' + MAX_LABEL + ' limit';

// A folder is refused, in the same words Alt+V uses: a directory has no one
// honest meaning as an upload, and sending only the files of a mixed selection
// would hand the agent something the user did not pick. Only a paste or a
// drop can carry a folder (a picker cannot select one), and there the item's
// filesystem entry is the authority whenever the browser exposes it -- its
// answer is final, either way. Without an entry, a folder shows up as an
// empty File the browser has no MIME type for, and that shape is refused.
// It is only a guess: an empty .toml, .log, .gitkeep or Makefile has the
// same shape, so it is consulted for a paste or a drop WITHOUT an entry and
// nowhere else.
const FOLDER = 'folders not supported - copy files';
function looksLikeFolder(f) { return f.size === 0 && !f.type; }
// The item's filesystem entry, or null where the browser has none to give (a
// synthetic item, a non-Chromium browser) -- never an exception mid-paste.
function entryOf(it) {
  try { return it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; }
  catch (e) { return null; }
}
function itemIsFolder(it) {
  const entry = entryOf(it);
  if (entry) return entry.isDirectory;
  const f = it.getAsFile();
  return !!(f && looksLikeFolder(f));
}

// Several files go as ONE request, one `file` part each, and the server makes
// ONE paste of all their paths -- the same shape as an Alt+V press. The reply
// lists what it saved; that count is what the page reports.
function sentLabel(d, fallback) {
  const n = (d.paths || []).length;
  return n > 1 ? n + ' files' : fallback;
}

const chosen = document.getElementById('proj-chosen');

pills.forEach(p => p.addEventListener('click', () => {
  pills.forEach(b => b.classList.remove('on'));
  p.classList.add('on');
  proj = p.dataset.name;
  chosen.textContent = 'project: ' + proj;
  chosen.className = 'chosen on';
  input.disabled = false;
  drop.className = 'drop ready';
  label.textContent = 'tap to select file';
  toast.textContent = '\u00a0';
  toast.className = 'toast';
}));

// --- type-to-filter project picker ---------------------------------------
//
// This page is used from a phone, so TAP stays the primary gesture: every pill
// is still a tap target and nothing below requires the keyboard. The text box
// is the second way in, for a fleet with more sessions than fit a thumb's
// scroll -- type a few letters, the list narrows, Enter takes the best match.
//
// Ranking mirrors the CLI picker's, so the same query picks the same project
// on both surfaces: case-insensitive, and scored by HOW a name matched rather
// than by how much of it did --
//
//   prefix          "api" -> apiserver
//   word boundary   "api" -> web-api          (a separator precedes the hit)
//   substring       "api" -> rapidly
//   subsequence     "api" -> alpha-pipeline   (a, p, i in order, not adjacent)
//
// Within one tier the order is the CONFIG's: the sort is stable and the pills
// start in config order, so a tie never reshuffles a list the user has already
// learned the shape of.
const T_PREFIX = 0, T_WORD = 1, T_SUB = 2, T_SUBSEQ = 3, T_NONE = 4;

function matchTier(name, query) {
  const n = String(name).toLowerCase(), q = query.toLowerCase();
  if (!q) return T_PREFIX;              // empty query: everything, config order
  if (n.startsWith(q)) return T_PREFIX;
  for (let i = 1; i < n.length; i++) {
    if (!/[a-z0-9]/.test(n[i - 1]) && n.startsWith(q, i)) return T_WORD;
  }
  if (n.includes(q)) return T_SUB;
  let k = 0;
  for (const ch of n) {
    if (ch === q[k] && ++k === q.length) return T_SUBSEQ;
  }
  return T_NONE;
}

const filterBox = document.getElementById('proj-filter');
const pillWrap = document.getElementById('pills');
const nomatch = document.getElementById('proj-nomatch');
const allPills = [...pills];
let shown = allPills.slice();
let hi = -1;

function setHighlight(idx) {
  hi = idx;
  allPills.forEach(p => p.classList.remove('hi'));
  if (hi >= 0 && shown[hi]) {
    shown[hi].classList.add('hi');
    shown[hi].scrollIntoView({block: 'nearest'});
  }
}

function renderFilter() {
  const q = filterBox.value.trim();
  const ranked = allPills
    .map((p, i) => ({p: p, i: i, t: matchTier(p.dataset.name, q)}))
    .filter(x => x.t !== T_NONE);
  ranked.sort((a, b) => a.t - b.t || a.i - b.i);
  shown = ranked.map(x => x.p);
  allPills.forEach(p => p.classList.add('off'));
  // Re-append in rank order: a hidden pill's position no longer matters, and
  // moving a node keeps its listeners, so the tap path is untouched.
  shown.forEach(p => { p.classList.remove('off'); pillWrap.appendChild(p); });
  nomatch.classList.toggle('show', shown.length === 0);
  setHighlight(shown.length ? 0 : -1);
}

filterBox.addEventListener('input', renderFilter);
filterBox.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!shown.length) return;
    e.preventDefault();
    const step = e.key === 'ArrowDown' ? 1 : -1;
    setHighlight(hi < 0 ? (step > 0 ? 0 : shown.length - 1)
                        : (hi + step + shown.length) % shown.length);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    // Selection goes through the pill's own click, so keyboard and thumb
    // reach the identical code path -- there is one way to pick a project.
    if (hi >= 0 && shown[hi]) shown[hi].click();
  } else if (e.key === 'Escape') {
    filterBox.value = '';
    renderFilter();
  }
});
// A tap picks whatever it landed on; the highlight follows so Enter after a
// tap can never mean a different project than the one on screen.
allPills.forEach(p => p.addEventListener('click', () => {
  const at = shown.indexOf(p);
  if (at >= 0) setHighlight(at);
}));

if (allPills.length) {
  renderFilter();
} else {
  // No sessions: the list says so already, and an input that can only ever
  // answer "no match" is noise.
  filterBox.style.display = 'none';
}

// Deep link: ?project=<name> (e.g. from a notification) pre-selects that
// project's pill on open, so a tap lands you straight on the right session.
(function () {
  const want = new URLSearchParams(location.search).get('project');
  if (!want) return;
  const pill = allPills.find(p => p.dataset.name === want);
  if (pill) { pill.click(); pill.scrollIntoView({block: 'center'}); }
})();

// A folder DROPPED on the picker reaches `change` as a plain File; the drop
// event, which fires first, still carries its items, so the decision is made
// there. A plain pick is never guessed at. Opening the picker forgets a drop
// that never became a selection.
let droppedFolder = false;
input.addEventListener('drop', e => {
  const items = (e.dataTransfer || {}).items || [];
  droppedFolder = [...items].some(it => it.kind === 'file' && itemIsFolder(it));
});
input.addEventListener('click', () => { droppedFolder = false; });

function pickFail(msg) {
  drop.className = 'drop err';
  label.textContent = msg;
  toast.textContent = msg;
  toast.className = 'toast err';
  input.value = '';
  resetDropSoon();
}

input.addEventListener('change', async () => {
  const files = [...input.files];
  const folder = droppedFolder;
  droppedFolder = false;
  if (!files.length || !proj) return;
  const what = files.length > 1 ? files.length + ' files' : files[0].name;
  if (folder) { pickFail(FOLDER); return; }
  const total = files.reduce((n, f) => n + f.size, 0);
  if (total > MAX_BYTES) { pickFail(what + ': ' + TOO_BIG); return; }
  drop.className = 'drop busy';
  label.textContent = what;

  const form = new FormData();
  for (const f of files) form.append('file', f);
  form.append('project', proj);
  form.append('inject', '1');

  try {
    const r = await fetch('/upload', {method:'POST', body:form});
    const d = await r.json();
    if (d.ok) {
      const pending = isPending(d);
      const sent = sentLabel(d, what);
      drop.className = pending ? 'drop ok pend' : 'drop ok';
      label.textContent = d.injected ? 'pasted into ' + proj
        : pending ? PEND_LABEL : sent;
      toast.textContent = sent + (pending ? PEND_NOTE : ' sent');
      toast.className = pending ? 'toast ok pend' : 'toast ok';
    } else {
      pickFail(d.error || 'failed');
      return;
    }
  } catch(e) {
    pickFail('network error');
    return;
  }
  input.value = '';
  resetDropSoon();
});

function resetDropSoon() {
  setTimeout(() => {
    if (drop.classList.contains('ok') || drop.classList.contains('err')) {
      drop.className = 'drop ready';
      label.textContent = 'tap to select file';
    }
  }, 2000);
}

// Ctrl+V clipboard upload: stage every pasted file (one image previews; one
// non-image, or several files, show as a tile naming them; plus the target
// project), send only on explicit confirm, and show live upload progress. XHR instead of
// fetch because only XHR exposes upload-progress events.
const pbox = document.getElementById('paste-box');
const pimg = document.getElementById('paste-img');
const pfile = document.getElementById('paste-file');
const pname = document.getElementById('paste-name');
const pdest = document.getElementById('paste-dest');
const psize = document.getElementById('paste-size');
const pbar = document.getElementById('paste-bar');
const pfill = document.getElementById('bar-fill');
const psend = document.getElementById('paste-send');
const pcancel = document.getElementById('paste-cancel');
let staged = null;
let sending = false;

function fmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

function refreshPaste() {
  if (!staged) return;
  pdest.textContent = proj ? '→ ' + proj : 'select a project above';
  psend.disabled = sending || !proj;
}
pills.forEach(p => p.addEventListener('click', refreshPaste));

function clearStage() {
  if (staged && staged.url) URL.revokeObjectURL(staged.url);
  staged = null;
  sending = false;
  pbox.className = 'paste';
  pbar.className = 'bar';
  pfill.style.width = '0%';
  psend.className = '';
  psend.textContent = 'Send';
  psend.disabled = true;
  pcancel.disabled = false;
}

window.addEventListener('paste', e => {
  if (sending) return;  // never swap the files out from under an upload
  const items = (e.clipboardData || {}).items || [];
  const files = [];
  let folder = false;
  for (const it of items) {
    // EVERY file item, of any type. A plain-text paste has no file item and
    // falls through to the browser untouched.
    if (it.kind !== 'file') continue;
    if (itemIsFolder(it)) { folder = true; continue; }
    const f = it.getAsFile();
    if (f) files.push(f);
  }
  if (!files.length && !folder) return;
  e.preventDefault();
  // One folder refuses the whole paste, exactly as it does an Alt+V press.
  if (folder) { refuse(FOLDER); return; }
  stageFiles(files);
});

function refuse(msg) {
  toast.textContent = msg;
  toast.className = 'toast err';
}

// A nameless pasted blob still needs a sensible extension on disk.
function extFor(type) {
  const sub = String(type || '').split('/')[1] || '';
  const known = {'jpeg': 'jpg', 'plain': 'txt', 'svg+xml': 'svg'};
  if (known[sub]) return known[sub];
  return /^[a-z0-9]+$/.test(sub) ? sub : 'bin';
}

function stageFiles(files) {
  const total = files.reduce((n, f) => n + f.size, 0);
  if (total > MAX_BYTES) {
    const what = files.length > 1 ? files.length + ' files'
      : (files[0].name || 'pasted file');
    refuse(what + ': ' + TOO_BIG);
    return;
  }
  if (staged && staged.url) URL.revokeObjectURL(staged.url);
  const ts = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  // The ORIGINAL name when the browser has one (a copied file does); only a
  // nameless blob gets a generated one (the server keeps same-named parts
  // apart, so two nameless blobs cannot collide).
  const list = files.map(f => ({
    file: f, name: f.name || ('paste-' + ts + '.' + extFor(f.type))}));
  const one = list.length === 1 ? list[0] : null;
  const isImage = !!one && String(one.file.type || '').startsWith('image/');
  staged = {files: list, url: isImage ? URL.createObjectURL(one.file) : null,
            label: one ? one.name : list.length + ' files'};
  // One image previews; anything else -- one non-image, or several files of
  // any kind -- is a tile naming what will be sent.
  if (isImage) {
    pimg.src = staged.url;
    pimg.style.display = '';
    pfile.className = 'paste-file';
  } else {
    pimg.removeAttribute('src');
    pimg.style.display = 'none';
    pname.textContent = one ? one.name
      : list.length + ' files: ' + list.map(s => s.name).join(', ');
    pfile.className = 'paste-file show';
  }
  psize.textContent = fmtSize(total);
  pfill.style.width = '0%';
  pbar.className = 'bar';
  psend.className = '';
  psend.textContent = 'Send';
  pbox.className = 'paste show';
  toast.textContent = '\u00a0';
  toast.className = 'toast';
  refreshPaste();
}

function pasteFail(msg) {
  sending = false;
  psend.className = 'err';
  psend.textContent = 'Retry';
  psend.disabled = false;
  pcancel.disabled = false;
  toast.textContent = msg;
  toast.className = 'toast err';
}

psend.addEventListener('click', () => {
  if (!staged || !proj || sending) return;
  sending = true;
  psend.className = '';
  psend.disabled = true;
  pcancel.disabled = true;
  psend.textContent = 'Sending 0%';
  pbar.className = 'bar show';

  const form = new FormData();
  for (const s of staged.files) form.append('file', s.file, s.name);
  form.append('project', proj);
  form.append('inject', '1');

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.addEventListener('progress', ev => {
    if (!ev.lengthComputable) return;
    const pct = Math.round(ev.loaded / ev.total * 100);
    pfill.style.width = pct + '%';
    psend.textContent = 'Sending ' + pct + '%';
  });
  xhr.addEventListener('load', () => {
    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status === 200 && d.ok) {
      pfill.style.width = '100%';
      const pending = isPending(d);
      psend.className = pending ? 'ok pend' : 'ok';
      psend.textContent = pending ? 'Saved, pasting...'
        : (d.injected ? 'Pasted into ' + proj : 'Sent') + ' ✓';
      toast.textContent = sentLabel(d, staged.label) + (pending ? PEND_NOTE
        : (d.injected ? ' pasted into ' + proj : ' sent'));
      toast.className = pending ? 'toast ok pend' : 'toast ok';
      setTimeout(clearStage, 2500);
    } else {
      pasteFail(d.error || 'upload failed');
    }
  });
  xhr.addEventListener('error', () => pasteFail('network error'));
  xhr.send(form);
});

pcancel.addEventListener('click', () => {
  if (sending) return;
  clearStage();
});
</script>

<script>
// Register the service worker only where it's allowed (HTTPS/localhost); over
// plain HTTP this is simply skipped, no errors.
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// Add-to-home-screen helper. Hidden once installed. On Android/HTTPS the
// beforeinstallprompt event gives a one-tap Install button; otherwise we show
// the platform's manual steps.
(function () {
  const box = document.getElementById('install');
  const text = document.getElementById('install-text');
  const btn = document.getElementById('install-btn');
  const standalone = window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
  if (standalone || localStorage.getItem('magent-install-hide')) return;

  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (ios) {
    // One-tap: install the Web Clip profile (drops the icon straight on the
    // Home Screen). Must be Safari; offer the manual route as a fallback.
    text.innerHTML = "Tap to install the app icon, then <b>Install</b> the profile. "
      + "(If it doesn't open, use this page in <b>Safari</b>, or Share &rsaquo; Add to Home Screen.)";
    btn.textContent = 'Install to Home Screen';
    btn.style.display = 'block';
    btn.addEventListener('click', () => { location.href = '/install.mobileconfig'; });
  } else {
    text.innerHTML = "Install: open the browser <b>menu</b> then <b>Add to Home screen</b> (or <b>Install app</b>).";
    let deferred = null;
    window.addEventListener('beforeinstallprompt', e => {
      e.preventDefault();
      deferred = e;
      text.innerHTML = "Install <b>magent upload</b> to your home screen for one-tap access.";
      btn.style.display = 'block';
    });
    btn.addEventListener('click', async () => {
      if (!deferred) return;
      deferred.prompt();
      await deferred.userChoice;
      deferred = null;
      box.classList.remove('show');
    });
  }
  box.classList.add('show');
  document.getElementById('install-x').addEventListener('click', () => {
    box.classList.remove('show');
    localStorage.setItem('magent-install-hide', '1');
  });
})();
</script>
</body>
</html>"""


def _sid(session: dict[str, object]) -> str:
    """Delegate to ``psmux.socket_id``."""
    return psmux.socket_id(session)


def _discover_sessions(config_path: str | None) -> list[dict[str, object]]:
    """Delegate to ``psmux.discover_sessions``."""
    return psmux.discover_sessions(config_path)


def _build_html(sessions: list[dict[str, object]]) -> str:
    pills = []
    cloud = psmux.cloud_pane_ids(sessions)
    for s in sessions:
        # A cloud pane takes no upload; offering it is a dead end. A cloud row
        # that SHADOWS a local twin (first-wins gave the pane to the local
        # agent) is no pill either: the twin's own row is the one pill.
        if _sid(s) in cloud or s.get("node") == "cloud":
            continue
        # data-name (the wire value posted back as `project`) is the psmux
        # socket id; the pill text shows the same id (P3-01 keeps the display
        # name only on the JSON surface, not the picker chrome).
        sid_esc = html.escape(_sid(s))
        pills.append(f'<div class="pill" data-name="{sid_esc}">{sid_esc}</div>')
    placeholder = (
        "\n".join(pills) if pills else '<p class="none">no active sessions</p>'
    )
    # The limit first: a session name is user text and must never be
    # rewritten by a later placeholder pass.
    page = _HTML_TEMPLATE.replace(
        "MAX_UPLOAD_BYTES_PLACEHOLDER", str(MAX_UPLOAD_BYTES)
    ).replace("MAX_UPLOAD_LABEL_PLACEHOLDER", upload_limit_text(MAX_UPLOAD_BYTES))
    return page.replace("PROJECTS_PLACEHOLDER", placeholder)


# --- PWA assets -----------------------------------------------------------

_MANIFEST = json.dumps(
    {
        "name": "magent upload",
        "short_name": "magent",
        "description": "Send files straight into your magent: sessions",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1e1e2e",
        "theme_color": "#1e1e2e",
        "icons": [
            {
                "src": "/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
).encode("utf-8")

# Cache the shell; never the dynamic session list or the upload endpoint.
_SERVICE_WORKER = b"""\
const C = 'magent-v1';
const SHELL = ['/icon-192.png', '/icon-512.png', '/manifest.webmanifest'];
self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).catch(() => {}));
});
// Drop every cache but the current one, so a renamed/bumped C never leaves an
// orphan behind on an already-installed client.
self.addEventListener('activate', e => e.waitUntil(
  caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k))))
    .catch(() => {})
    .then(() => self.clients.claim())
));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname === '/upload' || u.pathname === '/api/sessions'
      || u.pathname === '/install.mobileconfig' || u.pathname === '/health') return;
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(C).then(c => c.put(e.request, copy)).catch(() => {});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
"""

# Static PWA routes: (content-type, lazy bytes factory). Served with a long
# immutable cache since the icons/manifest/sw rarely change.
_PWA_ROUTES: dict[str, tuple[str, Callable[[], bytes]]] = {
    "/manifest.webmanifest": ("application/manifest+json", lambda: _MANIFEST),
    "/sw.js": ("application/javascript", lambda: _SERVICE_WORKER),
    "/icon-192.png": ("image/png", lambda: render_icon(192, True)),
    "/icon-512.png": ("image/png", lambda: render_icon(512, True)),
    "/icon-maskable-512.png": ("image/png", lambda: render_icon(512, False)),
    "/apple-touch-icon.png": ("image/png", lambda: render_icon(180, False)),
}

# Known routes per verb -- lets a wrong-method request on a real path answer 405
# (not 404), while a genuinely unknown path stays 404 (P3-16).
_GET_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "",
        "/api/sessions",
        "/api/cloud-panes",
        "/api/flash",
        "/install.mobileconfig",
        "/focus",
        "/health",
        *_PWA_ROUTES,
    }
)
_POST_PATHS: frozenset[str] = frozenset({"/upload"})

# iOS "Web Clip" configuration profile. Tapping a link to this in Safari prompts
# to install a profile that drops a Home Screen icon (our green arrow) opening
# the uploader -- a true one-tap install, no Share-sheet hunt. The target URL is
# built from the request's Host header so it matches whatever the phone typed
# (tailnet name + port). Fixed UUIDs so reinstalling replaces rather than dupes.
_WEBCLIP_UUID = "9D3B7E10-0001-4A00-9000-000000000001"
_PROFILE_UUID = "9D3B7E10-0002-4A00-9000-000000000002"


def _mobileconfig(host: str) -> bytes:
    import base64
    import html as _html

    icon_b64 = base64.b64encode(render_icon(180, False)).decode("ascii")
    url = _html.escape(f"http://{host}/")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>FullScreen</key><true/>
      <key>IgnoreManifestScope</key><true/>
      <key>Icon</key>
      <data>{icon_b64}</data>
      <key>IsRemovable</key><true/>
      <key>Label</key><string>magent upload</string>
      <key>PayloadDescription</key><string>Adds the magent upload Home Screen icon.</string>
      <key>PayloadDisplayName</key><string>magent upload</string>
      <key>PayloadIdentifier</key><string>ca.devino.magent.webclip</string>
      <key>PayloadType</key><string>com.apple.webClip.managed</string>
      <key>PayloadUUID</key><string>{_WEBCLIP_UUID}</string>
      <key>PayloadVersion</key><integer>1</integer>
      <key>Precomposed</key><true/>
      <key>URL</key><string>{url}</string>
    </dict>
  </array>
  <key>PayloadDescription</key><string>Install the magent upload app on your Home Screen.</string>
  <key>PayloadDisplayName</key><string>magent upload</string>
  <key>PayloadIdentifier</key><string>ca.devino.magent</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>{_PROFILE_UUID}</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
</plist>
""".encode()


# How long the HTTP response will wait for the paste before answering anyway.
#
# The file is already on disk by the time this wait starts, so everything past
# it is a courtesy: waiting a beat lets the overwhelmingly common fast paste be
# reported as the plain `injected: true` it is, and the bound is what stops a
# stalled multiplexer from turning a successful upload into a client timeout.
# Must stay comfortably under `altv.UPLOAD_HTTP_TIMEOUT_S` (a test pins that) --
# the whole defect being fixed here is a server that outlived its client's
# patience and left the user reading "upload failed" about a file that landed.
INJECT_GRACE_S = 3.0

# The whole life of one paste attempt, wherever it finishes. Deliberately ONE
# attempt: a `send-keys` that is merely slow is still in flight, and a retry on
# top of it pastes the same image twice into the agent's prompt. Past this the
# worker gives up and says so in upload.log, so a paste can never arrive
# minutes later on top of whatever the user did in the meantime.
INJECT_TIMEOUT_S = 60.0


def _inject_paste(project: str, text: str) -> tuple[bool, bool]:
    """Paste ``text`` -- the saved file paths as ONE line (``paths_line``) --
    into ``project``'s pane. Returns ``(injected, pending)``.

    The paste runs on its own thread and the caller waits only ``INJECT_GRACE_S``
    for it, because an HTTP handler must not be hostage to a multiplexer: this
    call used to be inline and unbounded, and a control command that stalled for
    74 s answered a listener that had given up at 20 s -- so a screenshot that
    was safely on disk, and that psmux eventually pasted, was reported to the
    user as "upload failed".

    The two flags are exhaustive and honest: ``(True, False)`` pasted,
    ``(False, True)`` still trying (the reply is early, not wrong), and
    ``(False, False)`` a real refusal the caller may name as one. Nothing is
    retried and nothing is re-sent -- see ``INJECT_TIMEOUT_S``.
    """
    log = get_logger("upload")
    done = threading.Event()
    outcome: list[bool] = []

    def _run() -> None:
        started = time.monotonic()
        try:
            # `literal`: the line is TEXT -- quoted paths, several of them --
            # and must never be read back as a psmux key name. Same `-l` wire
            # as the local Alt+V paste and `magent send`; no Enter is sent.
            pasted = psmux.send_keys(
                project, text, target=project, literal=True, timeout=INJECT_TIMEOUT_S
            )
            outcome.append(pasted)
        finally:
            done.set()
            elapsed = time.monotonic() - started
            if elapsed >= INJECT_GRACE_S:
                # The reply already said `inject_pending`; this line is the only
                # place that late verdict is recorded, so it is a WARNING and it
                # carries the wait it cost.
                log.warning(
                    "inject project=%s finished late after %.1fs pasted=%s",
                    project,
                    elapsed,
                    outcome[-1] if outcome else False,
                )

    threading.Thread(target=_run, name="magent-upload-inject", daemon=True).start()
    if done.wait(INJECT_GRACE_S):
        return (bool(outcome and outcome[0]), False)
    return (False, True)


# How much of the original name a saved file keeps, in UTF-8 bytes. One path
# component is capped at 255 (bytes on Linux, UTF-16 units on Windows), and
# `<stamp>_<n>_` rides in front; 150 also keeps the whole path under Windows'
# 260-character MAX_PATH from a typical home directory.
_NAME_MAX_BYTES = 150
# A "suffix" longer than this is not an extension worth keeping whole (a name
# like `notes.from-the-meeting-with-everyone`), so it is truncated as the stem.
_SUFFIX_MAX_BYTES = 20


def _saved_name(filename: str) -> str:
    """The part of a saved file's name that comes from the original: path
    stripped, anything but a word character, dot or dash made ``_`` (so no
    control character, quote, separator or line break survives into a pasted
    path), and capped at ``_NAME_MAX_BYTES`` by trimming the stem -- the
    suffix is what tells the agent what the file is, so it is kept.

    A dotfile keeps its name (``.env`` arrives as ``<stamp>_.env``; the prefix
    already stops it being hidden). Only a name that is nothing but dots
    becomes ``upload``, and trailing dots go: Windows drops them on create,
    and the returned path must name the file that exists.
    """
    basename = re.sub(r"[^\w.\-]", "_", Path(filename).name).rstrip(".")
    if not basename:
        return "upload"
    suffix = Path(basename).suffix
    if len(suffix.encode()) > _SUFFIX_MAX_BYTES:
        suffix = ""
    stem = basename[: len(basename) - len(suffix)]
    budget = _NAME_MAX_BYTES - len(suffix.encode())
    stem = stem.encode()[:budget].decode("utf-8", errors="ignore")
    if not suffix:
        stem = stem.rstrip(".")  # a cut can land on a dot, too
    return (stem + suffix) or "upload"


def _dest_for(upload_root: Path, stamp: int, filename: str) -> Path | None:
    """Reserve where one uploaded file lands and return it:
    ``<stamp>_<saved name>`` under the uploads dir, created empty, or ``None``
    if the name would escape it.

    Two files with one name must never overwrite each other -- the paste would
    then name one file twice -- whether they came in one request or in two in
    the same second. Only an exclusive create settles that across requests
    (an ``exists()`` check cannot see a name another request chose but has not
    written yet), so a clash bumps to ``<stamp>_<n>_<name>`` until one create
    wins. That also covers a case-insensitive filesystem, where ``A.txt`` and
    ``a.txt`` are one file. The caller writes into the reservation, and
    removes it if the request is refused.
    """
    basename = _saved_name(filename)
    name = f"{stamp}_{basename}"
    n = 1
    while True:
        dest = (upload_root / name).resolve()
        if not dest.is_relative_to(upload_root):
            return None
        try:
            with dest.open("xb"):
                return dest
        except FileExistsError:
            n += 1
            name = f"{stamp}_{n}_{basename}"


def _discard(dests: list[Path]) -> None:
    """Remove a refused request's reservations: best-effort, never raises."""
    for dest in dests:
        with contextlib.suppress(OSError):
            dest.unlink()


# Suffixes a single upload is announced as an "image" for. The phone's everyday
# upload is a screenshot, and its status-line confirmation predates any-file
# uploads -- it keeps reading exactly as it always did.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def _uploaded_what(file_count: int, suffix: str) -> str:
    """What a mobile upload's confirmation calls what arrived: ``image`` for
    one image, ``file`` for one anything-else, ``N files`` for several.
    ``suffix`` is the comma-joined suffix list the upload log line carries."""
    if file_count != 1:
        return f"{file_count} files"
    return "image" if suffix.lower() in _IMAGE_SUFFIXES else "file"


# A quoted filename taken whole, so a name with a `;` in it (legal on every OS,
# and now that any file uploads, a real case) is not cut at the `;` by the
# token split below. Browsers percent-encode a `"` inside it.
_FILENAME_RE = re.compile(r'\bfilename="([^"]*)"')


_BODY_CHUNK_BYTES = 256 * 1024


class UploadIncomplete(Exception):
    """The body ended before its declared length, or before the delimiter that
    closes its last part. What did arrive is not the file the user sent, so
    nothing of it is saved or pasted.

    ``received``/``declared`` carry the byte counts so the one log line the
    handler writes can say how far the client got before it went away."""

    def __init__(self, reason: str, *, received: int = 0, declared: int = 0) -> None:
        super().__init__(reason)
        self.received = received
        self.declared = declared


# Windows socket errors for "the peer went away": WSAECONNABORTED / WSAECONNRESET.
# Python maps them to ConnectionAbortedError / ConnectionResetError on its own,
# but an OSError built from a bare errno (a C-level path, a wrapped re-raise)
# keeps the raw number, so the numbers are checked too.
_CLIENT_GONE_ERRNOS = frozenset({10053, 10054, errno.EPIPE, errno.ECONNRESET})


def _client_went_away(exc: BaseException) -> bool:
    """True when ``exc`` means the CLIENT hung up mid-request (a phone off wifi,
    a listener that gave up), not that this server is broken.

    Such a fault is expected traffic: it is logged as one WARNING, never at
    exception level -- ERROR is what Sentry's logging integration captures, and
    a vanished peer is not an error in magent.
    """
    if isinstance(exc, ConnectionError):  # Aborted, Reset, BrokenPipe, Refused
        return True
    if not isinstance(exc, OSError):
        return False
    return (
        getattr(exc, "winerror", None) in _CLIENT_GONE_ERRNOS
        or exc.errno in _CLIENT_GONE_ERRNOS
    )


def _disposition(header_str: str) -> tuple[str, str]:
    """``(name, filename)`` from one part's headers."""
    name = ""
    filename = ""
    for line in header_str.split("\r\n"):
        if "Content-Disposition:" in line:
            for raw_token in line.split(";"):
                token = raw_token.strip()
                if token.startswith("name="):
                    name = token.split("=", 1)[1].strip('"')
                elif token.startswith("filename="):
                    filename = token.split("=", 1)[1].strip('"')
            quoted = _FILENAME_RE.search(line)
            if quoted:
                filename = quoted.group(1)
    return name, filename


def _next_delimiter(body: bytes | bytearray, delim: bytes, start: int) -> int:
    """Index of the CRLF that opens the next real delimiter at or after
    ``start``, or -1. A delimiter is ``CRLF--boundary`` followed by CRLF (another
    part) or ``--`` (the end); the same bytes followed by anything else are the
    file's own content."""
    needle = b"\r\n" + delim
    at = body.find(needle, start)
    while at >= 0:
        after = at + len(needle)
        if body.startswith((b"\r\n", b"--"), after):
            return at
        at = body.find(needle, at + 1)
    return -1


def _parse_multipart(
    handler: BaseHTTPRequestHandler,
) -> tuple[dict[str, str], dict[str, list[tuple[str, memoryview]]]]:
    """Minimal multipart/form-data parser. Returns (fields, files).

    ``files`` keeps EVERY part sent under a name, in order: one Alt+V press
    carries a whole Explorer selection as several ``file`` parts of one request.
    Each file's data is a ``memoryview`` into the one body read off the socket:
    at 100 MB a request, splitting and slicing copies held four bodies at once.

    Raises ``UploadIncomplete`` when fewer bytes arrived than were declared, or
    the closing delimiter never came -- a cut-short last part still has its
    headers, and saving it would announce a truncated file as uploaded.
    """
    content_type = handler.headers.get("Content-Type", "")
    if "boundary=" not in content_type:
        return {}, {}

    boundary = content_type.split("boundary=")[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]

    try:
        length = int(handler.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return {}, {}
    # Read in chunks (one raw recv each) rather than one rfile.read(length): when
    # the client vanishes mid-body the exception would carry none of what had
    # already arrived, and the log line needs the byte count.
    want = min(length, _request_limit())
    body = bytearray()
    try:
        while len(body) < want:
            chunk = handler.rfile.read1(min(_BODY_CHUNK_BYTES, want - len(body)))
            if not chunk:
                break
            body += chunk
    except OSError as exc:  # a stalled or reset client, mid-body
        raise UploadIncomplete(str(exc), received=len(body), declared=length) from exc
    if len(body) < length:
        raise UploadIncomplete(
            f"{len(body)} of {length} bytes arrived",
            received=len(body),
            declared=length,
        )

    view = memoryview(body)
    delim = f"--{boundary}".encode()
    fields: dict[str, str] = {}
    files: dict[str, list[tuple[str, memoryview]]] = {}

    # The first delimiter has no CRLF before it (it may follow a preamble).
    at = body.find(delim)
    while at >= 0:
        after = at + len(delim)
        if body.startswith(b"--", after):
            return fields, files  # the closing delimiter: the body is whole
        start = after + 2  # past the CRLF that ends the delimiter line
        end = _next_delimiter(body, delim, start)
        if end < 0:
            break
        head_end = body.find(b"\r\n\r\n", start, end)
        if head_end >= 0:
            name, filename = _disposition(
                str(view[start:head_end], "utf-8", errors="replace")
            )
            data = view[head_end + 4 : end]
            if filename:
                files.setdefault(name, []).append((filename, data))
            elif name:
                fields[name] = str(data, "utf-8", errors="replace")
        at = end + 2
    raise UploadIncomplete("the closing delimiter never arrived")


_FOCUS_TARGET_FILE = Path.home() / ".magent" / "focus-target"
_PICKER_ATTACHED_FILE = Path.home() / ".magent" / "picker-attached"


def _request_focus(project: str) -> None:
    """Ask the SSH session picker to switch to <project>: write a focus-target
    file and detach the picker's currently-attached client so its loop wakes,
    consumes the target, and re-attaches to it."""
    _FOCUS_TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FOCUS_TARGET_FILE.with_suffix(".tmp")
    tmp.write_text(project, encoding="utf-8")
    os.replace(tmp, _FOCUS_TARGET_FILE)
    try:
        current = _PICKER_ATTACHED_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if current:
        psmux.detach_client(current)


class UploadHandler(BaseHTTPRequestHandler):
    # Per socket OPERATION, not per request (StreamRequestHandler applies it
    # with settimeout): a slow 100 MB upload that keeps moving is never cut
    # off, but a client that declares a body and then stalls no longer pins a
    # handler thread -- and its partial buffer -- forever. A stall mid-body is
    # answered "Upload incomplete" and nothing is saved.
    timeout = CONNECTION_TIMEOUT_S
    config_path: str | None = None
    cached_sessions: ClassVar[list[dict[str, object]]] = []
    sessions_ts: float = 0
    port: int | None = None
    pid: int | None = None
    started_at: float = 0.0

    @staticmethod
    def _sessions_snapshot() -> list[dict[str, object]]:
        now = time.time()
        with _sessions_lock:
            if now - UploadHandler.sessions_ts > 10:
                UploadHandler.cached_sessions = _discover_sessions(
                    UploadHandler.config_path
                )
                UploadHandler.sessions_ts = now
            return UploadHandler.cached_sessions

    def _sessions(self) -> list[dict[str, object]]:
        return self._sessions_snapshot()

    def _send_bytes(self, data: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(data)

    def handle_one_request(self) -> None:
        # A peer that RSTs before (or between) requests raises out of the
        # request-line read, which is outside do_GET/do_POST's wrappers. The
        # stdlib would answer it with a traceback on the detached daemon's
        # invisible stderr; it is not a fault, so say nothing above DEBUG.
        try:
            super().handle_one_request()
        except Exception as exc:
            if not _client_went_away(exc):
                raise
            self.close_connection = True
            get_logger("upload").debug(
                "upload client went away between requests: %s", log_safe(repr(exc))
            )

    def do_GET(self) -> None:
        # Any unhandled error in a request handler must land in the "upload"
        # log at ERROR (-> logfile stack + Sentry), never only in the detached
        # daemon's invisible socketserver stderr (P2-03). The whole body is
        # wrapped, so even the pre-routing setup is covered.
        try:
            self._handle_get()
        except Exception as exc:
            log = get_logger("upload")
            if _client_went_away(exc):
                self.close_connection = True
                log.warning(
                    "upload client went away before the reply to GET %s: %s",
                    log_safe(self.path),
                    log_safe(repr(exc)),
                )
                return
            log.exception("GET handler crashed for %s", log_safe(self.path))
            with contextlib.suppress(OSError):
                self._json_response({"ok": False, "error": "internal"}, 500)

    def _handle_get(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path == "":
            self._send_bytes(
                _build_html(self._sessions()).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        elif path == "/api/sessions":
            # P3-04/P3-18: ok-envelope + the LIST lives under `sessions` (the
            # count is `session_count` on /health, never overloaded here).
            self._send_bytes(
                json.dumps({"ok": True, "sessions": self._sessions()}).encode(),
                "application/json",
            )
        elif path == "/api/cloud-panes":
            # Which panes are cloud ones is a CONFIG fact, so this answers from
            # the config alone: no psmux probe and no `_sessions_lock`. The
            # Alt+V listener asks it on every native / local-files press, and
            # /api/sessions is the wrong place -- a stale snapshot there is a
            # full has-session sweep (measured ~19s over 46 sockets) and its
            # list is LIVE-filtered, so a cloud pane whose probe flapped would
            # read as not-cloud and the press would paste into it.
            cloud_ids = psmux.cloud_pane_ids(
                psmux.config_sessions(UploadHandler.config_path)
            )
            self._send_bytes(
                json.dumps({"ok": True, "cloud_panes": sorted(cloud_ids)}).encode(),
                "application/json",
            )
        elif path == "/api/flash":
            # Status-line flash on behalf of a caller that has no screen of its
            # own -- today the hidden Alt+V/F2 listener, whose every failure was
            # otherwise only a line in hotkey.log. Deliberately unauthenticated,
            # like every other route here: the loopback + Tailscale bind IS the
            # access control (see DESIGN.md), and the blast radius of the worst
            # case is a clamped string on a status bar for _FLASH_MSG_MS.
            query = parse_qs(urlparse(self.path).query)
            flash_project = query.get("project", [""])[0]
            message = query.get("msg", [""])[0]
            if not flash_project or not message:
                self._json_response(
                    {"ok": False, "error": "project and msg are required"}, 400
                )
            else:
                clamped = message[:FLASH_MSG_MAX]
                # One INFO line per served flash. The caller is a hidden
                # process narrating into a status bar that keeps no history, so
                # without this "the status isn't showing" is unanswerable after
                # the fact: this says which phase messages arrived, and when.
                get_logger("upload").info(
                    "flash project=%s msg=%r", log_safe(flash_project), clamped
                )
                _flash(
                    None,
                    flash_project,
                    clamped,
                    _flash_duration(query.get("ms", [""])[0]),
                    style=_FLASH_TINTS.get(query.get("tint", [""])[0]),
                )
                # Answered only once psmux has the message, which is what paces
                # a caller flashing a SEQUENCE: it waits for each reply before
                # sending the next, so the phases cannot arrive out of order.
                self._json_response({"ok": True})
        elif path == "/install.mobileconfig":
            # Built per-request: the Web Clip URL must match the host:port the
            # phone actually used, which only the Host header knows.
            host = self.headers.get("Host", "localhost")
            data = _mobileconfig(host)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-apple-aspen-config")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="magent-upload.mobileconfig"',
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/focus":
            project = parse_qs(urlparse(self.path).query).get("project", [""])[0]
            if project in {_sid(s) for s in self._sessions()}:
                _request_focus(project)
                safe = html.escape(project)
                body = (
                    "<!doctype html><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width,initial-scale=1'>"
                    "<body style='margin:0;background:#1e1e2e;color:#cdd6f4;"
                    "font-family:system-ui;display:flex;align-items:center;"
                    "justify-content:center;height:100vh;text-align:center'>"
                    f"<div>Switched to <b style='color:#a6e3a1'>{safe}</b>.<br>"
                    "<span style='color:#6c7086;font-size:.85rem'>Open your terminal "
                    "(magent sessions) to continue.</span></div></body>"
                ).encode()
                self._send_bytes(body, "text/html; charset=utf-8")
            else:
                self._json_response({"ok": False, "error": "Unknown project"}, 404)
        elif path == "/health":
            uptime = (
                time.time() - UploadHandler.started_at
                if UploadHandler.started_at
                else 0.0
            )
            # Lock-free and sweep-free on purpose: this is a liveness probe, and
            # the sessions lock is held for as long as psmux takes to answer.
            # `sessions_ts` is written AFTER the list, so a nonzero stamp means
            # the list read below is a real sweep's result.
            swept_at = UploadHandler.sessions_ts
            body = json.dumps(
                {
                    "ok": True,
                    "service": "magent-upload",
                    "port": UploadHandler.port,
                    "pid": UploadHandler.pid,
                    "uptime_s": uptime,
                    # P3-18: a COUNT, named distinctly from the /api/sessions
                    # LIST. `null` = unknown (no sweep has landed yet), never a
                    # false 0; `sessions_age_s` says how old a known count is.
                    "session_count": (
                        len(UploadHandler.cached_sessions) if swept_at else None
                    ),
                    "sessions_age_s": (
                        max(0.0, time.time() - swept_at) if swept_at else None
                    ),
                }
            ).encode()
            self._send_bytes(body, "application/json")
        elif path in _PWA_ROUTES:
            content_type, factory = _PWA_ROUTES[path]
            self._send_bytes(factory(), content_type, cache=True)
        else:
            self._reject("GET", path)

    def do_POST(self) -> None:
        # See do_GET: a handler-thread crash (e.g. an OSError writing the
        # upload, an unexpected multipart fault) must page through logging at
        # ERROR and return a clean 500 -- the existing inner try/finally keeps
        # its inflight-count + outcome INFO line intact (P2-03).
        try:
            self._handle_post()
        except Exception as exc:
            log = get_logger("upload")
            if _client_went_away(exc):
                # The body was read in full (a mid-body loss is answered inside
                # _handle_post) so this is the reply write: the upload is on
                # disk and was pasted, only the answer had nowhere to go. One
                # WARNING line, no traceback, nothing for Sentry.
                self.close_connection = True
                log.warning(
                    "upload client went away before the reply to POST %s "
                    "(request body %s bytes): %s",
                    log_safe(self.path),
                    log_safe(self.headers.get("Content-Length", "?")),
                    log_safe(repr(exc)),
                )
                return
            log.exception("POST handler crashed for %s", log_safe(self.path))
            with contextlib.suppress(OSError):
                self._json_response({"ok": False, "error": "internal"}, 500)

    def _handle_post(self) -> None:
        log = get_logger("upload")
        parsed = urlparse(self.path)
        if parsed.path != "/upload":
            self._drain_request_body()  # reject-before-read: avoid a Windows RST
            self._reject("POST", parsed.path)
            return

        # Discovery is concurrent (sub-second), so validating against the session
        # cache no longer risks timing out the upload. The wire `project` is the
        # psmux socket id (P3-01), so we validate against `session` ids.
        sessions = self._sessions()
        valid_sessions = {_sid(s) for s in sessions}
        # A cloud pane is a local viewer; the agent runs in a VM that cannot
        # read ~/.magent/uploads on this PC (spec section 18.7). Asked of the
        # one first-wins answer, so a `[local, cloud]` pair stays a local pane.
        cloud_sessions = psmux.cloud_pane_ids(sessions)

        # ?project= marks an upload that already HAS a narrator: the Alt+V
        # listener flashed "Alt+V: capturing..." before it touched the clipboard
        # and will flash the specific outcome the moment this reply lands. The
        # status line is one line, so a second voice on it can only race the
        # first -- and the loser is whichever message the user needed. The
        # server therefore stays silent for flagged uploads and speaks only for
        # the mobile page, whose sender is looking at a phone, not at the bar.
        #
        # That silence extends to the DEFERRED paste verdict (`inject_pending`),
        # deliberately. The tempting fix -- flash "pasted" once the worker
        # finishes -- reintroduces the second writer under the exact condition
        # this code path exists for: the listener's own closing message is still
        # queued behind a slow status line when the worker lands, so the two
        # would race and the bar could show "pasted" and then "paste pending".
        # The late verdict goes to upload.log instead, and to the pane, where
        # the pasted path is its own proof.
        flagged = parse_qs(parsed.query).get("project", [""])[0]
        flagged = flagged if flagged in valid_sessions else ""

        ok = False
        project = flagged
        injected = False
        inject_pending = False
        byte_count = 0
        file_count = 0
        suffix = ""
        try:
            try:
                declared = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._drain_request_body()
                self._json_response({"ok": False, "error": "Bad Content-Length"}, 400)
                return
            if declared > _request_limit():
                self._drain_request_body()
                self._json_response(_too_large(), 413)
                return

            try:
                fields, files = _parse_multipart(self)
            except UploadIncomplete as exc:
                # A short body leaves the connection out of step with HTTP, and
                # the client may already be gone: close, and answer if it can
                # still hear.
                log.warning(
                    "upload client went away mid-body on %s after %d of %d bytes: %s",
                    log_safe(parsed.path),
                    exc.received,
                    exc.declared,
                    log_safe(str(exc)),
                )
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self._json_response(
                        {"ok": False, "error": "Upload incomplete"}, 400
                    )
                return
            project = fields.get("project", "") or flagged
            inject = fields.get("inject", "1") == "1"

            if "file" not in files or not project:
                self._json_response(
                    {"ok": False, "error": "Missing file or project"}, 400
                )
                return
            if project not in valid_sessions:
                self._json_response({"ok": False, "error": "Unknown project"}, 400)
                return
            if project in cloud_sessions:
                # Refused BEFORE a byte is written, with a flag Alt+V narrates
                # by name (altv "cloud-pane"). Only a name the server knows can
                # be a cloud pane, hence after the Unknown-project check.
                self._json_response(
                    {
                        "ok": False,
                        "cloud": True,
                        "error": (
                            "cloud session: attach images at claude.ai/code "
                            "or in the Claude app"
                        ),
                    },
                    409,
                )
                return

            parts = files["file"]
            byte_count = sum(len(data) for _name, data in parts)
            file_count = len(parts)
            suffix = ",".join(Path(name).suffix for name, _data in parts)
            if byte_count > MAX_UPLOAD_BYTES:
                # The files, not the envelope: the same sum the page and the
                # Alt+V listener checked, so the three can never disagree. The
                # body is already read, so there is nothing left to drain.
                self._json_response(_too_large(), 413)
                return

            _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            upload_root = _UPLOAD_DIR.resolve()
            stamp = int(time.time())
            dests: list[Path] = []
            # Every name is reserved before any byte is written, so an invalid
            # one refuses the request whole instead of leaving half of it
            # saved -- and a request that fails part-way takes its files back.
            try:
                for filename, _data in parts:
                    dest = _dest_for(upload_root, stamp, filename)
                    if dest is None:
                        _discard(dests)
                        self._json_response(
                            {"ok": False, "error": "Invalid filename"}, 400
                        )
                        return
                    dests.append(dest)
                for dest, (_name, data) in zip(dests, parts, strict=True):
                    dest.write_bytes(data)
            except BaseException:
                _discard(dests)
                raise

            if inject and psmux.find_psmux():
                injected, inject_pending = _inject_paste(
                    project, paths_line([str(d) for d in dests])
                )
            elif inject:
                log.warning(
                    "upload project=%s requested inject but psmux is unavailable",
                    log_safe(project),
                )

            ok = True
            self._json_response(
                {
                    "ok": True,
                    # `path` is the first file, as it always was; `paths` is
                    # every file, in the order they were sent.
                    "path": str(dests[0]),
                    "paths": [str(d) for d in dests],
                    "injected": injected,
                    # Three states, not two: pasted, definitely not pasted, and
                    # "still trying". A client that cannot tell the last two
                    # apart has to call a slow paste a failed upload.
                    "inject_pending": inject_pending,
                }
            )
        finally:
            # INFO outcome line -- project + counts + injected + suffixes only,
            # NEVER an original filename (personal data; F-hygiene).
            log.info(
                "upload project=%s ok=%s files=%d bytes=%d injected=%s pending=%s "
                "suffix=%s",
                log_safe(project),
                ok,
                file_count,
                byte_count,
                injected,
                inject_pending,
                suffix,
            )
            # Confirm in the same magent: status line -- for MOBILE uploads only.
            # A flagged (Alt+V) upload reports its own, more specific outcome;
            # see the `flagged` note above.
            done = project if project in valid_sessions else flagged
            # Never on a refused cloud pane: the phone page shows the refusal's
            # own text, and "upload failed" on the pane's bar would call a
            # deliberate refusal a fault.
            if done and not flagged and done not in cloud_sessions:
                if ok:
                    _flash(
                        None,
                        done,
                        f"magent  {_FB_OK} {_uploaded_what(file_count, suffix)} "
                        "uploaded",
                        _FLASH_OK_MS,
                        style=_MSG_GREEN,
                    )
                else:
                    _flash(
                        None,
                        done,
                        f"magent  {_FB_NO} upload failed",
                        _FLASH_NO_MS,
                        style=_MSG_RED,
                    )

    def _drain_request_body(self) -> None:
        """Discard the pending request body (bounded) before an early error
        response, and mark the connection to close.

        See the module-level "Rejected-request drain" note: on Windows an
        undrained body plus a socket close triggers a TCP RST, so the client
        sees a connection reset instead of our JSON error envelope. Bounded by
        ``_DRAIN_CAP_BYTES`` with a short per-read timeout so a lying, garbage,
        or endless Content-Length can never make us read forever; the connection
        is closed afterward so a partial drain is never reused as a next request.
        """
        self.close_connection = True
        try:
            declared = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            # Unparseable/absent length: best-effort drain up to the cap or EOF.
            declared = _DRAIN_CAP_BYTES
        remaining = max(0, min(declared, _DRAIN_CAP_BYTES))
        if not remaining:
            return
        prev_timeout = self.connection.gettimeout()
        self.connection.settimeout(_DRAIN_TIMEOUT_S)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(_DRAIN_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # Read timeout (nothing more is pending) or a reset mid-drain -- we
            # have pulled off what we can, which is enough to land the response.
            pass
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(prev_timeout)

    def _json_response(self, data: dict[str, object], status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, method: str, path: str) -> None:
        """405 when the path is a real route for the OTHER verb, else 404 --
        both as the shared JSON error envelope (P3-04/P3-16)."""
        wrong_method = (method == "GET" and path in _POST_PATHS) or (
            method == "POST" and path in _GET_PATHS
        )
        if wrong_method:
            self._json_response({"ok": False, "error": "Method not allowed"}, 405)
        else:
            self._json_response({"ok": False, "error": "Not found"}, 404)

    def log_message(self, fmt: str, *args: object) -> None:  # ty: ignore[invalid-method-override]  # reason: *args: object is a safe contravariant widening of *args: Any from BaseHTTPRequestHandler
        get_logger("upload").debug(fmt, *args)


# --- One port, one server -------------------------------------------------------
# ThreadingHTTPServer sets SO_REUSEADDR, and the option means two different
# things. On Linux it only lets a restart rebind past the previous server's
# TIME_WAIT connections; a second LIVE listener on the same address is still
# refused. On Windows it lets a second process bind a port that is already
# listening. Measured: two live servers co-listening on one port, both logging
# "listening ... :15505", the pid file naming only the later one -- so the
# watchdog killed or revived the wrong one and /health was answered by
# whichever the kernel picked. So on Windows the server claims the port with
# SO_EXCLUSIVEADDRUSE and no SO_REUSEADDR: a second serve -- a
# `--host 0.0.0.0` one included -- is refused. A restart still rebinds at
# once -- Windows never held a port hostage to TIME_WAIT connections (measured
# with ~20 of them on the port). POSIX keeps SO_REUSEADDR for exactly that
# restart.
#
# winsock2.h: SO_EXCLUSIVEADDRUSE is ((int)(~SO_REUSEADDR)), i.e. -5. Spelled
# out rather than read off ``socket`` because that name only exists on Windows,
# and the policy below is exercised on every OS.
_SO_EXCLUSIVEADDRUSE = -5
# WSAEACCES means two different things on a bind: an exclusive wildcard holder
# refusing a specific address, or a port Windows has reserved (a Hyper-V / WSL /
# Docker excluded range -- measured at 127.0.0.1:17000). Only a connect tells
# them apart, with the same 0.3s budget as launch._probe_upload_port.
_WSAEACCES = 10013
_HOLDER_PROBE_S = 0.3
_EXCLUDED_RANGES_HINT = "netsh int ipv4 show excludedportrange protocol=tcp"


def _claim_port_options(sock: socket.socket, platform: str = sys.platform) -> None:
    """Set the bind options that make a held port refuse a second server."""
    if platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _port_taken(exc: OSError) -> bool:
    """Whether a bind failed because another listener holds the port.

    ``errno.EADDRINUSE`` is the portable answer (on Windows it IS
    WSAEADDRINUSE, 10048). WSAEACCES is ambiguous and is ``_access_refused``'s.
    """
    return exc.errno == errno.EADDRINUSE


def _access_refused(exc: OSError, platform: str = sys.platform) -> bool:
    """Whether Windows refused the bind with WSAEACCES: held OR reserved."""
    return platform == "win32" and getattr(exc, "winerror", None) == _WSAEACCES


def _holder_answers(addr: str, port: int) -> bool:
    """True when something accepts a connection on the port ``addr`` was
    refused -- a holder, not a reservation. The wildcard is asked on loopback,
    where every wildcard holder also listens."""
    host = "127.0.0.1" if addr == "0.0.0.0" else addr
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(_HOLDER_PROBE_S)
    try:
        probe.connect((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


class BindFailed(RuntimeError):
    """run_server could not bind anything, so it never started serving.

    Its own type so the CLI shell can report it as a sentence instead of a
    traceback, without also swallowing a real crash of the serve loop.
    """


class PortInUse(BindFailed):
    """run_server's port is held by another listener -- usually another serve.

    Not a crash: a watchdog or ``--ensure`` spawn that loses a race to a server
    still starting is SUPPOSED to end here, quickly, leaving the winner alone.
    """


class _NoFqdnHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer minus http.server's reverse-DNS ``server_bind``.

    ``HTTPServer.server_bind`` resolves ``self.server_name`` via
    ``socket.getfqdn(host)`` -- a reverse-DNS lookup that macOS routes through
    mDNSResponder (``gethostbyaddr`` -> ``mdns_hostbyaddr``) and that can block
    INDEFINITELY when that daemon is slow or unresponsive. Observed wedged
    forever on macOS CI: the socket was bound but ``listen()`` was never
    reached, and macOS silently drops SYNs to a bound-unlistened port, so every
    client saw a hang (never a refusal) while the server looked alive. This
    server never uses ``server_name`` (no CGI; the ``Server:`` header comes
    from ``version_string()``), so the bind host is recorded verbatim and the
    resolver is never consulted.

    It also owns its bind options (see "One port, one server" above), so the
    stdlib's unconditional SO_REUSEADDR is switched off here.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        _claim_port_options(self.socket)
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


def _bind_addresses(host: str | None) -> list[str]:
    """Addresses run_server should bind.

    An explicit `host` (the `serve --host` escape hatch) is honored
    verbatim, including "0.0.0.0" for a user who knowingly wants a LAN-wide
    bind. Otherwise: loopback is always included -- the daily
    `_maybe_start_upload_server` liveness probe and the advertised
    `http://localhost:<port>` URL both depend on it -- plus the Tailscale IP
    when one is available. The LAN wildcard is never chosen automatically.
    """
    if host is not None:
        return [host]
    addrs = ["127.0.0.1"]
    ip = tailnet.ip4()
    if ip:
        addrs.append(ip)
    else:
        get_logger("upload").warning(
            "Tailscale IP unavailable; upload server bound to 127.0.0.1 only "
            "(phone upload disabled until Tailscale is up)."
        )
    return addrs


# --- Alt+V listener supervision ----------------------------------------------
# The listener used to be a ONE-SHOT spawn: whichever `magent --go` or `magent
# attach` ran last started it, and after that nothing ever looked again. A
# reboot, a crash, or a pip upgrade left Alt+V silently dead until the user
# happened to run attach again -- observed live: a listener last started 8 days
# and one reboot earlier, with the upload server still running and `status`
# reporting the whole thing as a benign default.
#
# serve is the right owner. It is the long-lived process the Alt+V chain already
# posts into, so "serve is up" and "Alt+V works" become one fact rather than two
# independent ones. The listener is deliberately NOT killed when serve stops:
# `down --all` already stops both (server first, listener second, so this
# supervisor is gone before the listener is), and a user restarting serve should
# not lose their hotkey in between.
HOTKEY_SUPERVISE_INTERVAL_S = 30.0


def local_url(bound_addrs: list[str], port: int) -> str:
    """The URL a process on THIS machine should use to reach this server.

    Loopback whenever it is reachable -- including under an explicit
    `--host 0.0.0.0`, which binds it -- so the listener never depends on
    Tailscale being up. Only a bind that deliberately excluded loopback
    (`serve --host <tailscale-ip>`) falls back to the address actually bound.
    """
    if "127.0.0.1" in bound_addrs or "0.0.0.0" in bound_addrs:
        return f"http://127.0.0.1:{port}"
    return f"http://{bound_addrs[0]}:{port}"


def supervision_enabled() -> bool:
    """Whether MAGENT_HOTKEY_SUPERVISOR permits serve to own the listener.

    Public because ``status``/``doctor`` must ask the same question the
    supervisor answers: a running server only implies a running listener if
    serve was actually allowed to supervise one. Reporting a DEAD listener to
    somebody who turned supervision off would be inventing a promise nobody
    made.

    A daemon must never die of a bad environment variable it does not use, and
    every other MAGENT_* consumer has already failed loudly at CLI entry by the
    time serve is running -- so an env that has gone bad underneath a detached
    process degrades to the default (supervise) with a log line, exactly as
    ``log._configured_level`` does for MAGENT_LOG_LEVEL.
    """
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env().hotkey_supervisor
    except ValidationError:
        get_logger("hotkey").warning(
            "supervisor: environment did not validate; supervising anyway"
        )
        return True


def _supervise_hotkey(
    server_url: str,
    stop_event: threading.Event,
    interval: float = HOTKEY_SUPERVISE_INTERVAL_S,
) -> None:
    """Keep an Alt+V listener alive for as long as this server runs.

    Runs on a daemon thread off ``run_server``. Every failure mode is a log line
    and another try next interval -- supervision must never be able to take down
    the server it rides on, which is the thing actually serving uploads.

    The lock is what stops two serve daemons (different ports, same machine)
    from racing each other into two listeners; it is deliberately NOT taken by
    the launch/attach spawn paths, so an interactive `magent attach` re-aiming
    the listener can never be blocked by a background supervisor.
    """
    from magent.platform import get_platform  # in-body: the OS backends are heavy

    if not get_platform().supports_hotkey():
        return
    log = get_logger("hotkey")
    if not supervision_enabled():
        log.info("supervisor: disabled by MAGENT_HOTKEY_SUPERVISOR")
        return
    # heavy subsystem: in-body per policy. launch owns the spawn recipe; this
    # module must not import the cli package (LS-A-001).
    from magent.launch import (
        SESSION0_HOTKEY_REFUSAL,
        ListenerWatch,
        ensure_hotkey_listener,
        session0_block,
    )

    # A serve running in Session 0 (a foreground `magent serve` over ssh) would
    # otherwise plant a detached listener there that outlives it -- and then
    # retry the refusal every interval for the life of the server. Say it once.
    refusal = session0_block(SESSION0_HOTKEY_REFUSAL)
    if refusal:
        log.warning("supervisor: %s", refusal)
        return

    # One watch for the life of the thread: the wedge confirm and the
    # replacement cooldown are remembered across ticks, not per call.
    watch = ListenerWatch()
    while True:
        try:
            with exclusive_lock("hotkey-supervisor"):
                if ensure_hotkey_listener(server_url, watch=watch) is None:
                    log.warning(
                        "supervisor: no Alt+V listener came up for %s; retrying in %ss",
                        server_url,
                        interval,
                    )
        except LockHeld:
            log.debug("supervisor: another server is supervising the listener")
        except Exception:
            log.exception("supervisor: Alt+V listener check failed")
        if stop_event.wait(interval):
            return


# --- psmux priority supervision ----------------------------------------------
# The third owner of ``psmux.boost_priority``, and on a real fleet the one that
# matters most: the launch path boosts what it just created, the attention
# daemon boosts on every poll -- but the attention daemon is frequently NOT
# running, while `magent serve` effectively always is (it is what every upload
# and every Alt+V press goes through, and `attention -d` revives it). A psmux
# server created by `magent attach`, by `up`, or by hand hours after the last
# bring-up would otherwise never be swept at all.
#
# Its own thread rather than a branch inside _supervise_hotkey, for one reason:
# that supervisor returns early on `MAGENT_HOTKEY_SUPERVISOR=0`, and somebody
# who owns their listener's lifetime has said nothing whatsoever about process
# priority. Same cadence, separate gate.
PSMUX_BOOST_INTERVAL_S = 30.0


def _supervise_psmux_priority(
    stop_event: threading.Event, interval: float = PSMUX_BOOST_INTERVAL_S
) -> None:
    """Keep the psmux fleet at above-normal priority for as long as serve runs.

    Runs on a daemon thread off ``run_server``. Idempotent and cheap (one
    Toolhelp snapshot plus an OpenProcess per psmux pid), so re-running it every
    interval costs milliseconds and is what makes a session created between two
    sweeps get boosted at all. Every failure is a log line and another try next
    interval -- this must never be able to take down the server it rides on.
    """
    if sys.platform != "win32":
        return  # priority classes are a Windows concept; nothing to sweep
    log = get_logger("launch")
    while True:
        try:
            psmux.boost_priority()
        except Exception:
            log.exception("psmux boost: priority sweep failed")
        if stop_event.wait(interval):
            return


# --- node sync supervision ---------------------------------------------------
# The fourth thing serve keeps alive, for the same reason as the other three:
# serve is the process that is always there. Its own thread and its own gate
# (MAGENT_NODE_SYNC); the "any project runs on a node" gate is read from the
# config file each interval -- through ConfigWatch, so only when it changed --
# which is how a node added to a running setup gets its daemon within a minute.
NODE_SYNC_SUPERVISE_INTERVAL_S = 60.0


def _supervise_node_sync(
    config_path: str | None,
    stop_event: threading.Event,
    interval: float = NODE_SYNC_SUPERVISE_INTERVAL_S,
) -> None:
    """Keep ``magent node sync`` running for as long as this server runs.

    Runs on a daemon thread off ``run_server``. Every failure is a log line and
    another try next interval. The lock stops two serve processes (different
    ports) from both spawning a daemon in the same instant; the daemon's own
    lock would settle it anyway, this just avoids the wasted process.
    """
    # heavy subsystem: in-body per policy. launch owns the spawn recipe; this
    # module must not import the cli package (LS-A-001).
    from magent.launch import ensure_node_sync, node_sync_env_enabled
    from magent.node_sync import SUPERVISOR_LOCK_NAME, ConfigWatch, DaemonLockUnknown
    from magent.paths import find_config

    log = get_logger("nodes")
    if not node_sync_env_enabled():
        log.info("node sync supervisor: disabled by MAGENT_NODE_SYNC")
        return
    watch: ConfigWatch | None = None
    while True:
        try:
            # Inside the try: with serve's cwd deleted and no --config,
            # find_config raises, and that is one failed tick, not a dead thread.
            if watch is None:
                watch = ConfigWatch(find_config(config_path))
            config = watch.current()
            if config is not None:
                with contextlib.ExitStack() as held:
                    # ONLY this lock means another serve is supervising; a
                    # LockHeld from anywhere else is a failed check below.
                    try:
                        held.enter_context(exclusive_lock(SUPERVISOR_LOCK_NAME))
                    except LockHeld:
                        log.debug(
                            "node sync supervisor: another server is supervising "
                            "the daemon"
                        )
                    except OSError as e:
                        # Its file would not open: skipped below, like the
                        # daemon's lock under ensure_node_sync.
                        raise DaemonLockUnknown(e) from e
                    # else, not a `continue` in the except: that would skip
                    # stop_event.wait(interval) below and spin the thread.
                    else:
                        ensure_node_sync(config, config_path)
        except DaemonLockUnknown as exc:
            # A lock file -- this one, or the daemon's -- would not open:
            # Windows answers EACCES while one is still pending delete. Known
            # and transient: not a failed check, and the next tick tries again.
            # A PermissionError from anywhere else (the config, the spawn) can
            # persist, and stays a failed check below.
            log.warning(
                "node sync supervisor: tick skipped (%s, errno %s): %s",
                type(exc.error).__name__,
                exc.error.errno,
                exc.error,
            )
        except Exception:
            log.exception("node sync supervisor: check failed")
        if stop_event.wait(interval):
            return


# How often serve's reaper thread sweeps for finished, long-idle local sessions.
# The threshold is at least 30 minutes, so a park lands within one interval of
# it; the first sweep waits one interval so it cannot race serve's own bring-up.
IDLE_REAP_INTERVAL_S = 300.0


def _supervise_idle_reap(
    config_path: str | None,
    stop_event: threading.Event,
    interval: float = IDLE_REAP_INTERVAL_S,
) -> None:
    """Park finished, long-idle local sessions for as long as serve runs.

    Runs on a daemon thread off ``run_server``, beside the Alt+V listener and
    the psmux priority sweep. It returns at startup, after one log line, only
    on what no config edit can change for this process: the
    ``MAGENT_IDLE_REAP`` kill switch and the two platform probes
    (``reap.process_off_reason``). Everything in the config is read per sweep:
    each one finds and reloads it, so ``settings.idleReap`` -- turned on,
    turned off, or a broken file fixed -- takes effect at the next sweep
    without a restart, and ``sweep_once`` applies the setting itself. A config
    that will not resolve or load (``find_config`` reads the working directory,
    which can be deleted under serve) skips that sweep with a warning naming
    why.

    Each sweep holds ``exclusive_lock("idle-reaper")`` so two serves on one box
    never sweep at once. Every failure is a log line and another try next
    interval -- this must never be able to take down the server it rides on.
    """
    from magent import config, paths, reap  # heavy subsystem: in-body per policy
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    log = get_logger("reap")
    plat = get_platform()
    off = reap.process_off_reason(plat)
    if off is not None:
        log.info("idle reaper %s", reap.off_phrase(off))
        return
    log.info("idle reaper on: sweeping every %.0fs", interval)
    while not stop_event.wait(interval):
        try:
            with exclusive_lock("idle-reaper"):
                try:
                    cfg = config.load_config(str(paths.find_config(config_path)))
                except (OSError, ValueError) as exc:
                    log.warning(
                        "idle reaper: config unreadable, skipping this sweep: %s", exc
                    )
                    continue
                parked = [r for r in reap.sweep_once(cfg, plat=plat) if r.parked]
                if parked:
                    log.info(
                        "idle reaper: parked %d session(s), freed~%dMB",
                        len(parked),
                        sum(r.freed for r in parked) // (1024 * 1024),
                    )
        except LockHeld:
            log.debug("idle reaper: another serve holds the sweep lock")
        except Exception:
            log.exception("idle reaper: sweep failed")


# --- Caller-supplied watchdogs ----------------------------------------------
# Hooks the `serve` command hands in, each run on its own daemon thread for as
# long as the server runs. Generic on purpose: the one in use today keeps the
# attention daemon alive (cli/attention_cmd.attention_watchdog), and that needs
# the daemon's pid/heartbeat/renderer judgement, which lives in the cli package
# this module must never import (LS-A-001). Same cadence as the two
# supervisors above.
WATCHDOG_INTERVAL_S = 30.0


def _run_watchdog(
    tick: Callable[[], None],
    stop_event: threading.Event,
    interval: float = WATCHDOG_INTERVAL_S,
) -> None:
    """Call ``tick`` now, then once an interval until ``stop_event`` is set.

    The first look is immediate: a serve that starts right after a restart is
    exactly when whatever it watches is missing. A tick that raises is a log
    line and another try next interval -- a watchdog must never be able to take
    down the server it rides on.
    """
    log = get_logger("upload")
    while True:
        try:
            tick()
        except Exception:
            log.exception("watchdog: check failed")
        if stop_event.wait(interval):
            return


def _serve_bind(server: ThreadingHTTPServer, log: logging.Logger) -> None:
    """``serve_forever`` for a SECONDARY bind, on its own daemon thread.

    Only the primary bind's loop can propagate to the CLI shell; a crash in the
    second one (the Tailscale address) would otherwise print a thread traceback
    to a console the daemon does not have and take that address down in total
    silence, with the loopback bind still answering /health. It is logged at
    exception level -- loud in the logfile, and captured by Sentry -- and not
    re-raised, because the server that is still serving must keep serving.
    """
    try:
        server.serve_forever()
    except Exception:
        log.exception("upload server: bind %s stopped serving", server.server_address)


def _warm_sessions() -> None:
    """Fill ``UploadHandler.cached_sessions`` once, at startup, off-thread.

    /health reports the cache and must never sweep psmux itself, so without
    this a fresh serve had no count to report until the first ``/sessions``
    request. Every failure is a log line: warming is an optimization of an
    honest ``null``, never a reason for serve to fall over."""
    try:
        UploadHandler._sessions_snapshot()
    except Exception:  # noqa: BLE001  # reason: a warm-up on a daemon thread must never kill serve; any discovery fault degrades to the honest null and a log line
        get_logger("upload").warning(
            "upload server: could not warm the session cache", exc_info=True
        )


def run_server(
    port: int = 8080,
    config_path: str | None = None,
    host: str | None = None,
    watchdogs: Sequence[Callable[[], None]] = (),
) -> None:
    log = get_logger("upload")
    UploadHandler.config_path = config_path

    servers: list[ThreadingHTTPServer] = []
    bound_addrs: list[str] = []
    reserved: list[str] = []
    for addr in _bind_addresses(host):
        try:
            servers.append(_NoFqdnHTTPServer((addr, port), UploadHandler))
            bound_addrs.append(addr)
        except OSError as e:
            refused = _access_refused(e)
            if not (_port_taken(e) or (refused and _holder_answers(addr, port))):
                if refused:
                    # Nobody holds it: a reservation, so there is no first
                    # server to defer to. Degrades like any unbindable address;
                    # if it was the only one, the ERROR below carries this.
                    why = (
                        f"port {port} is reserved or not permitted on {addr} "
                        f"({e}); see '{_EXCLUDED_RANGES_HINT}'"
                    )
                    reserved.append(why)
                    log.warning("upload server: %s", why)
                else:
                    log.warning("upload server: cannot bind %s:%d (%s)", addr, port, e)
                continue
            # Held on ANY of our addresses means somebody else is serving this
            # port. Serving the remainder would be two servers and one pid file
            # again, so give back what was bound and leave the holder alone --
            # its pid file is untouched (ours is only written after the bind).
            for s in servers:
                s.server_close()
            detail = (
                f"upload server: port {port} is already in use "
                f"({addr}: {e}); not starting a second server"
            )
            # WARNING, not ERROR: a spawn that lost a race to a server still
            # starting ends here by design, and is not a crash for Sentry.
            log.warning("%s", detail)
            raise PortInUse(detail) from e
    if not servers:
        # The one startup failure that is fatal rather than degraded. ERROR
        # level (not just the exception that follows) because a detached serve
        # has no console for the traceback to reach, and because ERROR is what
        # Sentry's logging integration captures -- see the crash-visibility
        # note on the serve loop below.
        detail = f"upload server: no bindable address on port {port}"
        if reserved:
            detail = f"{detail}: {'; '.join(reserved)}"
        log.error("%s", detail)
        raise BindFailed(detail)

    UploadHandler.port = port
    UploadHandler.pid = os.getpid()
    UploadHandler.started_at = time.time()
    log.info(
        "listening on %s:%d pid %d", ", ".join(bound_addrs), port, UploadHandler.pid
    )

    pidfile.write(_pid_path(port))

    for s in servers[1:]:
        threading.Thread(target=_serve_bind, args=(s, log), daemon=True).start()

    # /health's session count comes from this cache; fill it now rather than
    # when the first phone happens to load the page. Daemon: it must not hold
    # the process open, and psmux may be slow.
    threading.Thread(target=_warm_sessions, daemon=True).start()

    # Alt+V is only as alive as its listener, and nothing else in the product
    # ever re-checks it. Daemon thread: it must not hold the process open, and
    # a serve that is going down has nothing left to supervise anyway.
    hotkey_stop = threading.Event()
    threading.Thread(
        target=_supervise_hotkey,
        args=(local_url(bound_addrs, port), hotkey_stop),
        daemon=True,
    ).start()

    # ...and the typing latency of every pane is only as good as the priority of
    # the psmux processes carrying it. Same reasoning, same thread shape: serve
    # is the process that is always there, so it is the one that keeps sweeping.
    boost_stop = threading.Event()
    threading.Thread(
        target=_supervise_psmux_priority, args=(boost_stop,), daemon=True
    ).start()

    # ...and the node mirror is only as current as the daemon pulling it.
    node_sync_stop = threading.Event()
    threading.Thread(
        target=_supervise_node_sync, args=(config_path, node_sync_stop), daemon=True
    ).start()

    # ...and a finished session left idle for hours holds memory the rest of
    # the machine needs. Same owner for the same reason: serve is always up.
    reap_stop = threading.Event()
    threading.Thread(
        target=_supervise_idle_reap,
        args=(config_path, reap_stop),
        daemon=True,
        name="magent-reaper",
    ).start()

    # ...and whatever the command shell asked this server to keep an eye on.
    # After the bind, like the ones above: a serve that lost the port to another
    # one exits with PortInUse and must not have started anything on the way.
    watchdog_stop = threading.Event()
    for tick in watchdogs:
        threading.Thread(
            target=_run_watchdog, args=(tick, watchdog_stop), daemon=True
        ).start()

    # Why this is not a bare `try/finally` any more: serve died silently twice
    # in one day and left NOTHING behind -- no traceback (a detached process has
    # no console), no log line, only a pid file whose process was gone. The
    # `finally` logged the same "stopped" for a Ctrl+C and for a crash, so even
    # the log could not tell an operator which had happened. Every exit now
    # names its reason, and a crash is logged at exception level -- which is
    # also what hands it to Sentry (errors-only, logging integration at ERROR).
    # Nothing is swallowed: both handlers re-raise.
    reason = "loop returned"
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        reason = "keyboard interrupt"
        raise
    except Exception:
        reason = "crashed"
        log.exception("upload server crashed on port %d", port)
        raise
    finally:
        hotkey_stop.set()
        boost_stop.set()
        node_sync_stop.set()
        reap_stop.set()
        watchdog_stop.set()
        for s in servers[1:]:
            s.shutdown()  # called from a different thread than its serve_forever -> safe
        for s in servers:
            s.server_close()  # servers[0] exited via KeyboardInterrupt; just closes the socket
        pidfile.clear(_pid_path(port))
        log.info("stopped: %s", reason)
