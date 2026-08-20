"""Tiny upload server for mobile image transfer to psmux sessions."""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

from magent import psmux, tailnet
from magent.icons import render_icon
from magent.lockfile import LockHeld, exclusive_lock
from magent.log import get_logger
from magent.sessions import FLASH_MSG_MAX, FLASH_TINT_ERR, FLASH_TINT_OK


def _pid_path(port: int) -> Path:
    return Path.home() / ".magent" / f"upload_server-{port}.pid"


def server_pid(port: int) -> int | None:
    """Return the PID of the upload server recorded for this port, if any."""
    p = _pid_path(port)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def stop_server(port: int) -> bool:
    """Stop the upload server running on the given port. Returns True only if
    the kill actually succeeded. On failure the pid file is kept (not
    unlinked) so `status` or a retry can still find the process."""
    log = get_logger("upload")
    pid = server_pid(port)
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False
            )
            if result.returncode != 0:
                log.warning("taskkill pid %d failed rc=%d", pid, result.returncode)
                return False
        else:
            os.kill(pid, 15)
    except OSError:
        log.warning("failed to stop upload server pid %d", pid)
        return False
    with contextlib.suppress(OSError):
        _pid_path(port).unlink()
    return True


_UPLOAD_DIR = Path.home() / ".magent" / "uploads"

# Memory-exhaustion guard: reject a declared/actual body past this size
# instead of reading it all into memory. Not an auth control -- just an
# operability ceiling on the hot path.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

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
_DRAIN_CAP_BYTES = MAX_UPLOAD_BYTES
_DRAIN_CHUNK_BYTES = 64 * 1024
_DRAIN_TIMEOUT_S = 0.5

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
.pills{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.pill{background:#313244;border:1.5px solid #45475a;border-radius:20px;
  padding:6px 14px;font-size:.8rem;color:#bac2de;cursor:pointer;
  transition:all .12s;white-space:nowrap;-webkit-user-select:none;user-select:none}
.pill:active{transform:scale(.96)}
.pill.on{border-color:#89b4fa;background:#1e3a5f;color:#89b4fa;font-weight:600}
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
  <span>tap project &rsaquo; tap file or Ctrl+V &rsaquo; done</span>
</div>

<div class="pills" id="pills">PROJECTS_PLACEHOLDER</div>

<div class="drop" id="drop">
  <span id="drop-label">select a project first</span>
  <input type="file" id="file" accept="image/*,video/*,.pdf,.txt,.json,.csv,.log" disabled>
</div>

<div class="paste" id="paste-box">
  <img id="paste-img" alt="pasted image">
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

pills.forEach(p => p.addEventListener('click', () => {
  pills.forEach(b => b.classList.remove('on'));
  p.classList.add('on');
  proj = p.dataset.name;
  input.disabled = false;
  drop.className = 'drop ready';
  label.textContent = 'tap to select file';
  toast.textContent = '\u00a0';
  toast.className = 'toast';
}));

// Deep link: ?project=<name> (e.g. from a notification) pre-selects that
// project's pill on open, so a tap lands you straight on the right session.
(function () {
  const want = new URLSearchParams(location.search).get('project');
  if (!want) return;
  const pill = [...pills].find(p => p.dataset.name === want);
  if (pill) { pill.click(); pill.scrollIntoView({block: 'center'}); }
})();

input.addEventListener('change', async () => {
  if (!input.files.length || !proj) return;
  const file = input.files[0];
  drop.className = 'drop busy';
  label.textContent = file.name;

  const form = new FormData();
  form.append('file', file);
  form.append('project', proj);
  form.append('inject', '1');

  try {
    const r = await fetch('/upload', {method:'POST', body:form});
    const d = await r.json();
    if (d.ok) {
      const pending = isPending(d);
      drop.className = pending ? 'drop ok pend' : 'drop ok';
      label.textContent = d.injected ? 'pasted into ' + proj
        : pending ? PEND_LABEL : file.name;
      toast.textContent = file.name + (pending ? PEND_NOTE : ' sent');
      toast.className = pending ? 'toast ok pend' : 'toast ok';
    } else {
      drop.className = 'drop err';
      label.textContent = d.error || 'failed';
      toast.className = 'toast err';
    }
  } catch(e) {
    drop.className = 'drop err';
    label.textContent = 'network error';
    toast.className = 'toast err';
  }
  input.value = '';
  setTimeout(() => {
    if (drop.classList.contains('ok') || drop.classList.contains('err')) {
      drop.className = 'drop ready';
      label.textContent = 'tap to select file';
    }
  }, 2000);
});

// Ctrl+V clipboard upload: stage the pasted image (preview + target project),
// send only on explicit confirm, and show live upload progress. XHR instead of
// fetch because only XHR exposes upload-progress events.
const pbox = document.getElementById('paste-box');
const pimg = document.getElementById('paste-img');
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
  if (staged) URL.revokeObjectURL(staged.url);
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
  if (sending) return;  // never swap the image out from under an upload
  const items = (e.clipboardData || {}).items || [];
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      e.preventDefault();
      stageFile(it.getAsFile());
      return;
    }
  }
});

function stageFile(file) {
  if (staged) URL.revokeObjectURL(staged.url);
  const ext = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
  const ts = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  staged = {file: file, url: URL.createObjectURL(file),
            name: 'paste-' + ts + '.' + ext};
  pimg.src = staged.url;
  psize.textContent = fmtSize(file.size);
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
  form.append('file', staged.file, staged.name);
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
      toast.textContent = staged.name + (pending ? PEND_NOTE
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
    for s in sessions:
        # data-name (the wire value posted back as `project`) is the psmux
        # socket id; the pill text shows the same id (P3-01 keeps the display
        # name only on the JSON surface, not the picker chrome).
        sid_esc = html.escape(_sid(s))
        pills.append(f'<div class="pill" data-name="{sid_esc}">{sid_esc}</div>')
    placeholder = (
        "\n".join(pills) if pills else '<p class="none">no active sessions</p>'
    )
    return _HTML_TEMPLATE.replace("PROJECTS_PLACEHOLDER", placeholder)


# --- PWA assets -----------------------------------------------------------

_MANIFEST = json.dumps(
    {
        "name": "magent upload",
        "short_name": "magent",
        "description": "Send images straight into your magent: sessions",
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


def _inject_paste(project: str, dest: Path) -> tuple[bool, bool]:
    """Paste ``dest`` into ``project``'s pane. Returns ``(injected, pending)``.

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
            pasted = psmux.send_keys(
                project, str(dest), target=project, timeout=INJECT_TIMEOUT_S
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


def _parse_multipart(
    handler: BaseHTTPRequestHandler,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Minimal multipart/form-data parser. Returns (fields, files)."""
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
    body = handler.rfile.read(min(length, MAX_UPLOAD_BYTES))

    boundary_bytes = f"--{boundary}".encode()
    parts = body.split(boundary_bytes)

    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}

    for part in parts:
        if not part or part == b"--\r\n" or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_data, file_data = part.split(b"\r\n\r\n", 1)
        if file_data.endswith(b"\r\n"):
            file_data = file_data[:-2]

        header_str = header_data.decode("utf-8", errors="replace")
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

        if filename:
            files[name] = (filename, file_data)
        elif name:
            fields[name] = file_data.decode("utf-8", errors="replace")

    return fields, files


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
    config_path: str | None = None
    cached_sessions: ClassVar[list[dict[str, object]]] = []
    sessions_ts: float = 0
    port: int | None = None
    pid: int | None = None
    started_at: float = 0.0

    def _sessions(self) -> list[dict[str, object]]:
        now = time.time()
        with _sessions_lock:
            if now - UploadHandler.sessions_ts > 10:
                UploadHandler.cached_sessions = _discover_sessions(
                    UploadHandler.config_path
                )
                UploadHandler.sessions_ts = now
            return UploadHandler.cached_sessions

    def _send_bytes(self, data: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        # Any unhandled error in a request handler must land in the "upload"
        # log at ERROR (-> logfile stack + Sentry), never only in the detached
        # daemon's invisible socketserver stderr (P2-03). The whole body is
        # wrapped, so even the pre-routing setup is covered.
        try:
            self._handle_get()
        except Exception:
            log = get_logger("upload")
            log.exception("GET handler crashed for %s", self.path)
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
                    "flash project=%s msg=%r", flash_project, clamped
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
            body = json.dumps(
                {
                    "ok": True,
                    "service": "magent-upload",
                    "port": UploadHandler.port,
                    "pid": UploadHandler.pid,
                    "uptime_s": uptime,
                    # P3-18: a COUNT, named distinctly from the /api/sessions LIST.
                    "session_count": len(UploadHandler.cached_sessions),
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
        except Exception:
            log = get_logger("upload")
            log.exception("POST handler crashed for %s", self.path)
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
        valid_sessions = {_sid(s) for s in self._sessions()}

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
        suffix = ""
        try:
            try:
                declared = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._drain_request_body()
                self._json_response({"ok": False, "error": "Bad Content-Length"}, 400)
                return
            if declared > MAX_UPLOAD_BYTES:
                self._drain_request_body()
                self._json_response({"ok": False, "error": "File too large"}, 413)
                return

            fields, files = _parse_multipart(self)
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

            filename, data = files["file"]
            byte_count = len(data)
            suffix = Path(filename).suffix
            basename = Path(filename).name.replace(" ", "_")
            basename = re.sub(r"[^\w.\-]", "_", basename)
            if not basename or basename.startswith("."):
                basename = "upload"
            safe_name = f"{int(time.time())}_{basename}"

            _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = (_UPLOAD_DIR / safe_name).resolve()
            if not dest.is_relative_to(_UPLOAD_DIR.resolve()):
                self._json_response({"ok": False, "error": "Invalid filename"}, 400)
                return
            dest.write_bytes(data)

            if inject and psmux.find_psmux():
                injected, inject_pending = _inject_paste(project, dest)
            elif inject:
                log.warning(
                    "upload project=%s requested inject but psmux is unavailable",
                    project,
                )

            ok = True
            self._json_response(
                {
                    "ok": True,
                    "path": str(dest),
                    "injected": injected,
                    # Three states, not two: pasted, definitely not pasted, and
                    # "still trying". A client that cannot tell the last two
                    # apart has to call a slow paste a failed upload.
                    "inject_pending": inject_pending,
                }
            )
        finally:
            # INFO outcome line -- project + byte-count + injected + suffix only,
            # NEVER the original filename (personal data; F-hygiene).
            log.info(
                "upload project=%s ok=%s bytes=%d injected=%s pending=%s suffix=%s",
                project,
                ok,
                byte_count,
                injected,
                inject_pending,
                suffix,
            )
            # Confirm in the same magent: status line -- for MOBILE uploads only.
            # A flagged (Alt+V) upload reports its own, more specific outcome;
            # see the `flagged` note above.
            done = project if project in valid_sessions else flagged
            if done and not flagged:
                if ok:
                    _flash(
                        None,
                        done,
                        f"magent  {_FB_OK} image uploaded",
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
    """

    def server_bind(self) -> None:
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
    from magent.launch import ensure_hotkey_listener

    while True:
        try:
            with exclusive_lock("hotkey-supervisor"):
                if ensure_hotkey_listener(server_url) is None:
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


def run_server(
    port: int = 8080, config_path: str | None = None, host: str | None = None
) -> None:
    log = get_logger("upload")
    UploadHandler.config_path = config_path

    servers: list[ThreadingHTTPServer] = []
    bound_addrs: list[str] = []
    for addr in _bind_addresses(host):
        try:
            servers.append(_NoFqdnHTTPServer((addr, port), UploadHandler))
            bound_addrs.append(addr)
        except OSError as e:
            log.warning("upload server: cannot bind %s:%d (%s)", addr, port, e)
    if not servers:
        # The one startup failure that is fatal rather than degraded. ERROR
        # level (not just the exception that follows) because a detached serve
        # has no console for the traceback to reach, and because ERROR is what
        # Sentry's logging integration captures -- see the crash-visibility
        # note on the serve loop below.
        detail = f"upload server: no bindable address on port {port}"
        log.error("%s", detail)
        raise RuntimeError(detail)

    UploadHandler.port = port
    UploadHandler.pid = os.getpid()
    UploadHandler.started_at = time.time()
    log.info(
        "listening on %s:%d pid %d", ", ".join(bound_addrs), port, UploadHandler.pid
    )

    pid_file = _pid_path(port)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    for s in servers[1:]:
        threading.Thread(target=_serve_bind, args=(s, log), daemon=True).start()

    # Alt+V is only as alive as its listener, and nothing else in the product
    # ever re-checks it. Daemon thread: it must not hold the process open, and
    # a serve that is going down has nothing left to supervise anyway.
    hotkey_stop = threading.Event()
    threading.Thread(
        target=_supervise_hotkey,
        args=(local_url(bound_addrs, port), hotkey_stop),
        daemon=True,
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
        for s in servers[1:]:
            s.shutdown()  # called from a different thread than its serve_forever -> safe
        for s in servers:
            s.server_close()  # servers[0] exited via KeyboardInterrupt; just closes the socket
        with contextlib.suppress(OSError):
            pid_file.unlink()
        log.info("stopped: %s", reason)
