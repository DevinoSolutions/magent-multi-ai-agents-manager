# magent — Design Record

This document records how magent is built and *why it is shaped the way it
is*, for an AI agent picking up this codebase cold. It is a design record,
not a wishlist: it describes what the code on disk actually does. Where the
shape looks wrong at first glance, that is usually because it was
adjudicated on purpose during a formal multi-stage audit (2026-07) — this
document exists so that adjudication is not re-litigated by a future agent
who wasn't there. Aspirational changes live only in the Known Debt section.
Audit IDs in parentheses (`R9`, `ADJ-S2-4`, `NF-S3-003`, ...) are provenance
tags from that audit; the substance of every decision is stated here in
full, so nothing in this file requires the (untracked) audit artifacts to
understand.

Decision lens used throughout the audit that produced this record:
maintainability > operability > performance, optimized for a cold agent's
legibility, with sub-lenses of modularity, deduplication, clarity, and
convention-following.

## 1. Module map

### Dependency direction

```
pure leaves:  grid · paths · style · titles · log · terminals · agent_state · config
                          ^
subsystems:   tiling · platform/ · sessions/ · discover · init_config · launch · upload_server · hotkey
                          ^
cli/ command modules:  app · config_io · ui · background · config_editor · menu · attach · docs · mobile · session_picker · status
                          ^
cli/__init__.py  (registration hub)
```

Arrows point from dependent to dependency (imports flow upward in this
list). Each layer only imports from layers below it, with one documented
exception (the `app.py` cycle-break, below) and one documented sibling edge
(`menu.py` imports `config_editor.py` directly, one-directional — the config
editor never imports the menu back; the rationale is written in `menu.py`'s
own docstring).

### The registration hub and the cycle it breaks

`cli/__init__.py` is a 24-line registration hub and nothing else: it imports
`app.main`, then imports every other command module (`attach`, `config_editor`,
`config_io`, `mobile`, `docs`, `menu`, `session_picker`, `background`, `status`,
`ui`) purely so their `@main.command` decorators fire at import time, then
re-exports the ~16 underscore-prefixed names that tests and other call sites
still reach via `magent.cli.<name>`. It never imports `paths` or `style`
directly — those are top-level modules, not part of the `cli` package.

`main` (the click group) lives **alone** in `cli/app.py`, importing nothing
from sibling command modules at its own top level. This is deliberate: since
the hub eagerly imports every command module (to register it), and every
command module needs to `from magent.cli.app import main` to attach its
own commands, any command module importing back from `app.py` at top level
would be a real import cycle. `app.py`'s no-subcommand interactive path (the
menu, `--edit`, `--init`, attach-flow dispatch) needs several sibling
handlers — `_attach_flow`, `_menu_down`, `_menu_status`, `_menu_up`,
`_run_discovery`, `_run_sessions_picker`, `_show_menu` — so `main`'s body
imports them from `magent.cli` (the hub) **inside the function**, after
all registration has already completed. This in-body import is the
documented cycle-break, not an oversight.

### Pure leaves

None of these imports any other `magent` module (`style.py` imports
`click`; the rest are stdlib-only):

- **`grid.py`** — `Rect`/`MonitorRect`/`TileSlot` dataclasses + `compute_grid`,
  the DPI-aware tiling-slot math (caps columns/rows per monitor so no tile
  falls below Windows Terminal's minimum shrink size — `MIN_TILE_W`/
  `MIN_TILE_H` with the measured rationale in the comment above them).
- **`paths.py`** — config-file location only, stdlib-only. Its own docstring
  records *why* it must live at the top level and not as `cli/paths.py`:
  `upload_server.py` needs `find_config` without depending on the `cli`
  *package* (the hub imports every command module for registration; if the
  config-path leaf lived inside `cli`, `upload_server` would depend back on
  the very package that transitively pulls it in) — this is the structural
  fix for what used to be a latent `cli`↔`upload_server` load cycle (LS-A-001).
- **`altv.py`** — one Alt+V press, from chord to outcome: the phase
  narration, the upload, the closed outcome vocabulary and the single FIFO
  flash pump. Imports `log` and `sessions` only. It is deliberately NOT part
  of `hotkey.py`: that module raises `ImportError` off win32, and everything
  here is plain sockets and strings, so keeping it separate is what makes the
  press pipeline importable — and testable against a real `magent serve` — on
  Linux and macOS.
- **`style.py`** — `style = click.style`, a one-line shared shortcut. It used
  to be independently defined twice (once in the old monolithic `cli.py`,
  once in `launch.py`); both call sites now import `style` from here
  (LS-A-003). A transitional `S` alias existed during the multi-PR migration
  and has since been deleted repo-wide — every call site uses `style` directly.
- **`titles.py`** — owns `MAGENT_TITLE_PREFIX = "magent:"` plus `generate_titles`/
  `get_leaf_name` (LS-B-006). This is the single source of truth for the
  `magent:`-prefixed window-title convention: `cli/attach.py` builds titles from
  it (the only two build sites), `hotkey.py` strips it to recover the
  project name.
- **`log.py`** — rotating file logging (`get_logger`, one logger + one log
  file per named concern under `~/.magent/logs/`) and cross-platform
  liveness heartbeats (`write_heartbeat`/`heartbeat_fresh`). Heartbeats live
  here rather than in `hotkey.py` specifically so platform-agnostic callers
  (`status`, Linux CI) can check daemon liveness without importing the
  Windows-only hotkey module. Logging setup is best-effort by design — a
  failure falls back to `NullHandler` rather than raising, because the
  daemons that call it run detached with no console to crash to. One log
  file per *concern*, not per process: several magent processes write the
  same name concurrently, which is why the handler is
  `_SharedRotatingFileHandler` — see §2 "One log file, many processes".
- **`terminals.py`** — `detect_terminal()` + per-OS terminal-priority lists.
  Note for a cold agent: no `src/` module currently calls it —
  `platform/linux.py` and `platform/macos.py` each hard-code their own
  `shutil.which(...)` terminal-priority chain inline instead of calling this
  leaf. It is exercised only by tests. This wasn't raised as a finding in
  the audit that produced this document; flagged here for whoever looks next.
- **`agent_state.py`** — file-per-session lifecycle store (`working`/`done`/
  `needs-input`/`error`/`idle`, written by the agent's lifecycle hooks, plus
  `parked`, written by magent's idle reaper and never by the hooks), keyed by
  a hash of the session's normalized cwd. Stdlib-only by design (its own
  docstring: it's imported from hook handlers on the hot path of every agent
  turn, so it must stay dependency-light). Has zero tests today (see Known
  Debt).
- **`config.py`** — grouped with subsystems below for its behavioral role,
  but structurally a leaf (no `magent`-internal imports).

### Subsystems

- **`config.py`** — one dataclass schema (`MagentConfig`/`Settings`/
  `ProjectConfig`/`LayoutConfig`/`SSHConfig`), one envelope factory
  (`default_config`), one pair of serializers (`layout_to_dict`/
  `settings_to_dict`) that every config generator delegates through, a pure
  `load_config` reader, and the pure `migrate_config_text` (no function in
  the module writes to disk). `DEFAULT_TOOLS` is the one dict of built-in
  tool commands (`claude`, `codex`, `cursor-agent`, `agy`); `Settings.tools`'
  default factory and `_parse_settings`'s fallback both copy it
  (`dict(DEFAULT_TOOLS)`) rather than sharing one mutable dict (LS-B-002).
- **`tiling.py`** — the *one* window resolve-and-place loop, shared by
  `launch.run_magent`'s post-launch tiling and `cli/attach.py`'s
  `_tile_titles` (R13). Before this module existed the two call sites each
  hand-rolled their own snapshot/retry loop with independently-drifted magic
  numbers. Its retry constants are named and centralized:
  `RETRY_SECS_CONTAINS` (20s — `contains`-mode matches like VS Code windows
  are slow to appear), `RETRY_SECS_EXACT` (6s), `POLL_INTERVAL_S` (1.0s).
  `place_windows` takes an immediate snapshot, places everything already
  visible, then polls only the still-missing set up to the slower of the two
  deadlines, logging a WARNING via `get_logger("launch")` for anything still
  missing before invoking the caller's `on_missing` callback. Its optional
  `deadline_s` overrides that per-mode budget, and it is a deadline for
  latecomers — **never an up-front wait**. There is deliberately no pre-sleep:
  the attach path used to pass a blind `settle_s` scaled to the window count,
  so a 40-window attach whose psmux sessions already existed sat on untiled
  windows for a fixed 40s even though all 40 were up in under a second.
- **`platform/` (ABC + per-OS backends)** — `Platform` declares the
  cross-platform contract via `@abstractmethod` (`set_dpi_aware`,
  `list_monitors`, `find_window`, `move_window`, `launch_terminal`,
  `launch_vscode`) plus concrete-with-safe-default methods a backend may
  leave unoverridden: `launch_psmux_session`/`attach_psmux` (default `raise
  NotImplementedError("psmux is only supported on Windows")`) and the
  capability probes `supports_psmux()`/`supports_hotkey()` (both default
  `False`). `snapshot_windows` also carries an ABC default (`{}`), but **all
  three backends now override it** — Windows via `EnumWindows`, Linux via
  `wmctrl -l` (xdotool fallback), macOS via a tab-delimited System Events pass
  — because it is the window resolver `tiling.place_windows` calls to find the
  handles it moves; the bare `{}` default silently disabled the launch-path
  auto-tiling on Linux/macOS (every window resolved as "not found"). All three
  backends implement the six abstract methods; **only `WindowsPlatform`**
  overrides the psmux methods and capability probes —
  `LinuxPlatform`/`MacOSPlatform` inherit those ABC defaults as-is.
  `find_window`'s `mode` parameter is typed `Literal["exact", "contains"]`
  on the ABC and all three implementations, and each implementation raises
  `ValueError` on an unrecognized mode string before any OS dispatch, so a
  bogus mode fails fast instead of reaching a live `osascript`/`xdotool`
  call (LS-B-005). `get_platform()` picks the concrete backend by
  `sys.platform` and imports it lazily, so importing `magent.platform`
  never pulls in Windows- or macOS-specific code on the wrong OS.
- **`sessions/`** — `AGENT_TOOLS: dict[str, AgentTool]` is the registry of
  per-tool resumability (`claude`, `codex` today). `AgentTool` is a frozen
  dataclass: `session_ids` (a `(project_dir, count, config_dir) ->
  list[str|None]` callable), `resume_command`, and `happy` (whether the tool can be wrapped
  with the `happy` mobile/web relay); `multi_window` is a derived property
  (`session_ids is not None`). `build_resume_command` is the one dispatcher;
  an unregistered tool falls back to its own base command unchanged.
  `sessions/claude.py` and `sessions/codex.py` each implement the same two
  free functions (`get_<tool>_session_ids`, `build_<tool>_resume`) against
  that tool's own on-disk session format — the registry is what lets
  `launch.py` and `cli/` treat every registered tool identically (F-CT-001).
- **`discover.py`** — finds candidate projects from Claude/Codex/VS Code
  history and merges them by path. `_merge_candidate` keeps whichever
  candidate has the strictly greatest `last_active` seen so far, ties going
  to the first offered — the fix for a bug where a two-way pairwise merge
  could silently prefer a strictly older source (R9). Depends on `config`
  (for `default_config`/`_derive_tab_color`) and `sessions.claude` (for its
  path-encoding helper).
- **`init_config.py`** — the `--init --base-dir` folder-scan generator
  (`scan_for_projects`/`generate_config`/`write_config`); delegates to
  `config.default_config`/`_derive_tab_color` so its output can't drift from
  `discover.py`'s (F-D5-003).
- **`launch.py`** — the widest-importing subsystem: `config`, `grid`, `log`,
  `platform`, `sessions`, `style`, `tiling`, `titles`. `run_magent` is now
  a 5-phase composition shell — radon A(4), down from F(83) pre-audit —
  (`_prepare_grid` → `_select_projects` → `_launch_projects` →
  `_start_psmux_and_upload` → `_tile_targets`), each phase returning data or
  `None`; the shell alone owns the command's exit code and the
  no-monitors/empty-group echoes. `_launch_projects` further splits its
  per-project dispatch along the IDE/CLI-agent seam into
  `_dispatch_ide_project` and `_dispatch_cli_agent_project` (the latter is,
  at radon D(27), the most complex function remaining in the module — known
  and measured, not hidden). `_tile_targets` is a thin delegate to
  `tiling.place_windows`; no resolve/retry logic is re-implemented in
  `launch.py`. The psmux bring-up-and-spawn-upload-server phase is named
  **`_start_psmux_and_upload`** rather than `_bring_up_psmux`, specifically
  to avoid colliding one-underscore-apart with the already-existing public
  `bring_up_psmux` (the attach-path's headless detached-session creator,
  used by `up_cmd`/`_menu_up`/`_attach_flow`) — those are two different
  operations, and giving them near-identical names would have been its own
  clarity defect.
- **`upload_server.py`** — imports the `psmux` and `tailnet` modules,
  `icons.render_icon`, and `log.get_logger` at the top level, reaching every
  psmux primitive (`find_psmux`, `send_keys`, `discover_sessions`, …) through
  the `magent.psmux` module (consolidated in #39) rather than the former
  top-level `launch._psmux_session_name` import, and **never** imports the
  `cli` package (that is the actual invariant LS-A-001 established — not
  "depends on nothing but `paths`," which was an earlier, imprecise
  description this document deliberately does not repeat). `run_server` binds
  one `ThreadingHTTPServer` per address returned by `_bind_addresses` (see Key
  Decisions).
- **`reap.py`** — the idle reaper (see Key Decisions, "A finished, long-idle
  agent is parked, not killed"). Split in two: a pure core (`Signals` →
  `decide` → `"reap"` or one of the closed `VETO_REASONS`; `threshold_s`,
  `quiet_s`, `off_reason`) tested with no processes and no clock, and a thin
  gather/act layer (`gather`, `_read_one`, `_stop`, `_park`) around it.
  `sweep_once` is the one public entry, and `upload_server.
  _supervise_idle_reap` its only production caller. Imports `agent_state`,
  `env`, `log` and `procs` at the top; `psmux`, `fleet`, `sessions` and the
  platform in-body.
- **`hotkey.py`** — the Windows-only Alt+V clipboard-image listener.
  `if sys.platform != "win32": raise ImportError(...)` fires at import time,
  by design — every call site imports it lazily, behind a `supports_hotkey()`
  gate, with a `# ImportError off-Windows (hotkey.py guards); must stay lazy`
  comment at the import. Imports only `log` and `titles` from `magent`.
- **`attach_client.py`** — the reconnecting ssh supervisor that runs inside
  every `magent attach` pane, shipped as its own `magent-attach-client`
  console script (see Key Decisions). Imports `magent.style` and `magent.titles`
  (plus `magent.env` in-body, for `spawn_attach_window`); `argparse`
  is imported in-body because `cli/attach.py` imports this module at the top
  level (for `spawn_attach_window` / `client_exe` / the multiplexer constants /
  the client exe name) and the registration hub would otherwise put argparse
  on `magent --help`'s critical path. It owns the two strings `cli/attach.py`'s
  corpse detection is coupled to — the ssh connection options
  (`SSH_CONNECTION_OPTS`) and the remote attach command — so the marker
  `_attach_markers` scans for and the command a pane actually runs cannot
  drift apart.

### `cli/` command modules

Each imports `main` from `cli/app.py` (to attach its own commands) plus
whatever subsystems and sibling `cli/` leaves it needs. "Heavy" subsystem
imports (`launch`, `upload_server`, `discover`, `agent_state`, the platform
backends via `get_platform()`, and the lazy `hotkey` import) are placed
**inside function bodies**, each with a one-line why-comment (`# heavy
subsystem: in-body per policy`, or the hotkey-specific ImportError comment)
— see Key Decisions for why this is a deliberate policy, not scattered
laziness.

- **`app.py`** — `main` alone (see above).
- **`config_io.py`** — the raw-dict config I/O leaf: `_load_raw_config`/
  `_save_raw_config` (round-trips the on-disk JSON as a plain `dict`,
  preserving every key including ones the typed schema doesn't model) plus
  `_load_config_or_exit` (wraps `config.load_config`, the typed path, catching
  `(ValueError, FileNotFoundError)` — `ConfigError` is a `ValueError`
  subclass so it's caught without a separate except clause — and exiting 1
  with a plain `Error: <msg>` on stderr). See Key Decisions for why both
  paths are kept.
- **`ui.py`** — pure presentation (banner/menu chrome, grid preview, session
  listing) plus exactly two platform-guarded helpers, each guarded in-body:
  `_force_utf8_console` (Windows-only ctypes) and `_print_qr` (optional
  `qrcode` import inside a `try/except ImportError` that prints an install
  tip on failure — a deliberate optional dependency, not a latent bug;
  ADJ-S2-5).
- **`background.py`** — the runtime-probe/daemon-bootstrap leaf: port/pid
  liveness checks (`_probe_port`, `_pid_alive`, `_running_upload_port`) and
  the detached-process launchers for the upload server and the Alt+V
  listener (`_maybe_start_upload_server`, `_maybe_start_hotkey`). Also owns
  `_tailnet_host` (Tailscale MagicDNS name → Tailscale IP → LAN IP
  fallback, used by `mobile_cmd` in `mobile.py`).
- **`config_editor.py`** — `_config_menu` (the single worst-graded function
  in the repo — see Key Decisions) and the `config` command group (14
  subcommands, including `migrate`). Imports the raw-dict path from
  `config_io` (`_load_raw_config`/`_save_raw_config`), never the typed
  loader — the interactive editor's whole reason for existing is to preserve
  unknown keys the typed schema would drop.
- **`menu.py`** — the interactive main menu (`_show_menu`) and the first-run
  discovery wizard (`_run_discovery`). Imports `config_editor` directly at
  its own top level to reach `_config_menu` — the one documented sibling
  import in the `cli/` package (documented in `menu.py`'s docstring), safe
  because `config_editor.py` never imports back from `menu.py`.
- **`attach.py`** — SSH/attach orchestration: `_attach_flow` (see Key
  Decisions), its no-mux sibling `_attach_nomux`, `_tile_titles` (delegates
  to `tiling.place_windows` with a `deadline_s` that scales with the window
  count — see Known Debt), and the `up`/`attach`/`hotkey` commands. Loads typed
  config through `config_io._load_config_or_exit`, whose `as_json` mode powers
  `up --json`'s JSON error envelope (see Key Decisions).
- **`docs.py`** — the `magent docs` command: a pure-string Markdown
  generator (~190 content lines) for the full config reference, reading live
  defaults off `config.LayoutConfig`/`config.Settings` — including the
  example-config `tools` block, now derived from the factory defaults so it
  can't drift from `DEFAULT_TOOLS` (NF-S3-003, resolved pass-2).
- **`mobile.py`** — `serve`/`mobile`/`termius` commands. `serve` carries the
  `--host` escape hatch (see Key Decisions).
- **`session_picker.py`** — live psmux session listing (`sessions_cmd`) and
  the looping attach-and-return picker (`_run_sessions_picker`). Named
  `session_picker`, not `sessions`, to avoid confusion with the top-level
  `magent.sessions` package (recorded at extraction time).
- **`status.py`** — `_render_status` (shared by the `status` command and the
  menu's `_menu_status`) plus the `down` command. Owns the daemon-health
  probes: `_health_check` (HTTP GET `/health` — proves the upload server is
  actually *serving*, not just that a pid or port looks alive),
  `_upload_state`/`_listener_state`/`_gather_status`/`_is_degraded`, and the
  `status --json`/exit-3-on-degraded contract (exit codes: 0 healthy, 1
  config missing/invalid, 3 degraded; click itself uses 2 for usage errors).

## 2. Key decisions

Each of these looks like it could be "cleaned up." Each was examined and
left as-is on purpose. Do not refactor these without re-reading the
rationale.

**Two-path config contract, by design (ADJ-S2-4).** `config_io.py`'s
`_load_raw_config`/`_save_raw_config` round-trip the on-disk config as a
plain `dict`, deliberately kept separate from `config.load_config` (the
validated, typed path used everywhere else). `config.py` ships no typed
*writer*, and `load_config` intentionally drops/warns-on unknown keys rather
than modeling them. If the interactive config editor (`config_editor.py`)
round-tripped a save through the typed dataclasses instead, any key the
schema doesn't know about would be silently dropped from the user's file.
The two paths are the fix, not the disease. Anyone who "deduplicates" the
editor onto `load_config`/a typed writer will cause silent data loss for any
hand-added or forward-compatible config key.

**`load_config` never writes; nothing in `config.py` does (R10).**
`load_config` is a pure read: on a schema version below current, it
prints `Warning: config schema v<N> < v<CURRENT>; run: magent config
migrate` to stderr and returns in-memory data — it never touches the file.
Persisting a migration (or backfilled colors) requires `magent config
migrate`, which migrates in memory (`migrate_config_text`) and writes through
`config_io.save` under the config lock. A load that
rewrites the file as a side effect was one of the audited defects; do not
reintroduce it.

**Color backfill is ephemeral until migrated.** `load_config` calls
`_backfill_colors` on every load, deriving a color for any project missing one
— in memory only, never written back. The derivation is DETERMINISTIC
(`_derive_tab_color` hashes the project's title/path → HLS hue, golden-angle
collision-avoidance within a config), so a colorless project shows the SAME
color every run (P3-07); `magent config migrate` (or a config-editor save)
still persists it into the file so external readers see it and it becomes
editable. This keeps `load_config` a pure read; run `migrate` once to pin.

**The dotenv file is `~/.magent/.env` (`env.ENV_FILE`) — never the
CWD's `.env`.** magent is a launcher: it is run from arbitrary project
directories, and nearly every real project directory carries a `.env` of its
own. pydantic-settings loads *every* key of a dotenv file (prefixed or not),
so with the closed schema (`extra="forbid"`) a CWD-relative `env_file`
hard-failed startup on any foreign project's innocent keys — a day-one field
incident: running `magent` inside an eBay project rejected that project's
`EBAY_*` tokens, and the then-current error formatter rebranded them
`MAGENT_EBAY_*`, names that existed in no file. Hence: the dotenv lives
in magent's own home dir (beside logs/state), where every key is
legitimately magent's to police; `extra="forbid"` stays; foreign extras
report under their raw names via `env.validation_error_items` (shared by
`app.py` and `doctor`), and the startup hint names the file. Anyone
"restoring" CWD dotenv support for dev convenience will reintroduce the
incident.

**The `--json` config-error envelope is unified through
`config_io._load_config_or_exit` (NF-S3-005, resolved pass-2).** The helper
takes an `as_json` flag: on a config-load failure it emits
`{"ok": false, "error": "<msg>"}` as JSON **on stdout** (so a machine caller
reading `--json` always gets JSON, never a stderr `Error: <msg>` line or a
raw traceback); without the flag it keeps the plain-text `Error: <msg>` on
stderr for human callers. Both `status --json` and `up --json` route through
it — neither keeps a raw `config.load_config` call of its own, so the former
"`up_cmd` is the one permitted raw-loader site outside `config_io.py`"
exception is gone, and `status --json`'s old plain-text asymmetry with it is
gone too.

**`_config_menu` (F(48), `cli/config_editor.py`), `main` (E(33),
`cli/app.py`), and `_attach_flow` (D(29), `cli/attach.py`) were relocated,
not decomposed — on purpose.** All three moved out of the former
2,400+-line monolithic `cli.py` into their current modules with their bodies
otherwise untouched, each behind a characterization test that pins its
current behavior. Decomposing any of them is legitimate next-cycle work, but
it must start from that pin, not from a fresh read of the function. High
complexity here is known, measured, and fenced — not an oversight awaiting a
quick fix.

**The Alt+V hook calls `GetWindowTextW` from inside the low-level keyboard
hook, and that's an accepted risk, not a bug (F-D4-003).** `hotkey.py`'s
`get_active_window_title` is called from `_hook_decide` — but only on the
Alt+V chord itself (`kb.vkCode == VK_V and state["alt_held"]`), not on every
keystroke. The risk is accepted because Windows' own `LowLevelHooksTimeout`
bounds how long any single hook invocation can stall the input pipeline, and
the hook callback (`_make_hook_proc`'s wrapper around `_hook_decide`) is
fully exception-wrapped and **always** calls `user32.CallNextHookEx` on both
success and exception paths, so a failure here cannot break systemwide
keyboard input. The minimal future hardening (swap to `SendMessageTimeoutW`)
is recorded in Known Debt, not treated as a live bug.

**The upload server binds loopback + Tailscale, not `0.0.0.0`, and there is
deliberately no auth token (R7 trim).** `upload_server._bind_addresses`
always includes `127.0.0.1` (the local liveness probe and the advertised
`localhost` URL depend on it — its docstring states the constraint) and
appends the machine's Tailscale IPv4 when available; the LAN wildcard is
never chosen automatically, and a warning is logged when Tailscale is
unavailable and the server ends up loopback-only. The bind set *is* the
access control — this is a single-user, opt-in tool, and a shared-secret
token was explicitly triaged out of scope (recorded as open debt, not
forgotten). `serve --host` (including an explicit `0.0.0.0`) is the
documented escape hatch. Non-Tailscale LAN devices losing access to the
uploader is the **intended** behavior of this change, not a regression.

**`hotkey.py` raises `ImportError` at import time off-Windows, by design.**
`if sys.platform != "win32": raise ImportError("hotkey module is
Windows-only")` runs at module import. Every caller (`cli/attach.py`,
`cli/status.py`, `cli/background.py`) imports it lazily, inside a function body,
behind a `get_platform().supports_hotkey()` check, each with a `# ImportError
off-Windows (hotkey.py guards); must stay lazy` comment on the import line.
Hoisting any of these imports to module level breaks `import magent.cli`
on Linux/macOS.

**The in-body "heavy subsystem" import policy in `cli/` exists because the
registration hub is eager.** `cli/__init__.py` imports every command module
at package-import time (to fire its `@main.command` decorators), so any
subsystem a command module imports at its own top level is paid for on
every `magent` invocation, including `magent --help`. `launch`,
`upload_server`, `discover`, and `agent_state` are therefore imported
**inside function bodies** in `cli/` command modules, each carrying a
`# heavy subsystem: in-body per policy` comment. Verified at the tree this
document ships with: `import magent.cli` loads none of
`magent.launch`/`upload_server`/`discover`/`agent_state`. (The
`magent.platform` package `__init__` *is* loaded — `cli/attach.py`
imports `tiling`, which needs the `Platform` type — but that module is a
lightweight ABC + lazy factory; the actually-heavy OS backends
(`platform/windows.py`'s ctypes bindings, etc.) import only when
`get_platform()` is called.)

**The ruff ruleset is a curated, expanded pack (`[tool.ruff.lint]`), no
longer just the `E4, E7, E9, F` audited baseline.** The baseline stays first
in the `select` list (pinned explicitly, immune to ruff's floating defaults);
everything after it is the pre-refactor hardening pack, each group carrying a
one-line why in `pyproject.toml`: hygiene (`W`/`I`/`UP`/`B`/`A`),
simplification & return/raise discipline (`C4`/`SIM`/`RET`/`RSE`/`ISC`/`PIE`),
the complexity ceilings (`C90` + `PLR0912`/`PLR0915`, seeded at the Phase-0
measured max and ratcheted **down** only, never up), the loudness pack
(`T20`/`BLE`/`S110`/`S112`/`TRY`/`LOG`/`G`/`DTZ` — nothing fails silently, so
every error stays Sentry-capturable), and drift guards (`ERA`/`TC`/`TID`/`RUF`,
where `RUF100` is the unused-noqa rot guard). The gate lints `src` + `tests` +
`scripts`; the only sanctioned softening is `[tool.ruff.lint.per-file-ignores]`,
one reason-comment per code — nothing from `src/` goes there. Changing the
`select`/`ignore` list requires a written reason in this file, per house rule.
`ANN401` (no `Any` in annotations) is active in the `select` list now, not
deferred — `Any` elimination happens under ty, the sole type checker (mypy
was retired; see Key Decisions).

**Help-snapshot tests normalize one verified Click difference rather than
pinning a Click version.** `tests/unit/test_cli_structure.py::_normalize_help`
rewrites `[OPTIONS] [COMMAND] [ARGS]...` to `[OPTIONS] COMMAND [ARGS]...`
before comparing — Click 8.4 brackets the metavar for
`invoke_without_command=True` groups (this repo's bare `main --help`), Click
8.3 does not, and this machine's two reachable interpreters resolve
different Click versions. The normalization is a single verified substring,
so the snapshots stay byte-sensitive to everything else (a reparented
command, changed help text). Pinning one Click version would only trade an
environment-dependent false failure for flakiness elsewhere.

**`AGENT_TOOLS` covers deep CLI agents only; IDE tool identity is still
string-matched, on purpose for now (F-CT-003).** `sessions.AGENT_TOOLS` only
knows about `claude`/`codex` — the tools that can resume a specific session.
Whether a project's tool is an IDE (`vscode`/`cursor`/`code`) is still
checked with literal membership tests repeated in `launch.py`,
`upload_server.py`, and `cli/session_picker.py`. This is deferred
consolidation debt (an `IDE_TOOLS` registry is the natural sibling to
`AGENT_TOOLS`), listed below because it's real — not an oversight nobody
noticed, and not something to hot-fix in an unrelated PR.

**mypy retired 2026-07-06 (commit `719d17e`); ty is now the sole type
checker.** Running two type checkers meant two suppression dialects for the
same class of finding — a `# type: ignore` here, a `# ty: ignore` there, for
what is conceptually one problem. Consolidating onto `ty==0.0.56` keeps that
surface singular. The accepted risk is depending on a pre-1.0 checker with
known false positives (documented in CLAUDE.md's gotchas); revisit this
decision once ty ships a 1.0 release.

**`platform/windows.py` and `hotkey.py` are excluded from the main ty pass
(win32 ctypes symbols unresolvable under the host-platform view on Linux)
and checked by a dedicated `ty --python-platform win32` step instead (added
2026-07-07) — full type coverage on every host; if ty's platform emulation
regresses pre-1.0, fall back to a scoped 2-file mypy backstop.**

**`tests/` is not yet under ty.** The gate's ty step only checks `src` and
`scripts` (`ty check src scripts ...`) — `tests/` is staged, tracked future
work (spec §6.5), not an oversight; ruff (lint + format) does cover `tests/`
today.

**All magent windows share one title grammar (2026-07-07, 0-users breaking
change): `magent:` + optional `[!]`/`[x]`/`[+]` badge + name.** Before this, only
the attach path emitted `magent:` titles and every consumer did its own string
work (hotkey stripped the prefix, tiling matched exact full titles) — which
made in-place title *rewrites* (the attention daemon's state badges)
impossible without breaking resolution. Now `titles.make_title` is the only
producer and `titles.parse_title` the only consumer (hotkey routing, tiling's
`magent-name` mode), so a
badge in the title is invisible to matching. The badge
sits at the FRONT because taskbars truncate title tails; working/idle
deliberately render unbadged (quiet title = nothing needs you). psmux session
names remain unprefixed — the grammar applies at the window-title boundary
only. Constraint: project names must not start with the `[?] ` shape.

**Dependency scanning is a separate advisory workflow, not a quality-gate
step (added 2026-07-07).** `.github/workflows/dependency-audit.yml` runs a
pinned `pip-audit==2.10.1` over the exported `uv.lock` closure whenever
dependencies change and on a weekly schedule (advisories are published
without commits), and `.github/dependabot.yml` files weekly version-update
PRs (uv, github-actions, npm — each still gated by the required quality
check). It is deliberately NOT wired into `scripts/check.py`: the gate must
stay deterministic and offline-runnable, and advisory-database state is
external — a new CVE should surface loudly on its own schedule, not
retroactively turn an unrelated commit red at pre-push. The same reasoning
keeps the job out of the branch ruleset's required checks initially; promote
it once its flake rate is known.

**Attach panes are supervised, and the supervisor is a console script, not a
subcommand (added 2026-08-09).** An attach pane used to be `wt -- ssh -t
<target> "psmux -L <sid> attach || magent sessions <sid>"`. The first
disconnect killed it dead: OpenSSH exits 255, Windows Terminal keeps the pane,
and the user was left closing forty `[process exited with code 255]` terminals
by hand before re-running `magent attach`. `attach_client.py` now runs between
wt and ssh and redials on transport failure. Three decisions inside that are
easy to "clean up" wrongly:

*Why a separate entry point.* `magent-attach-client` exists for exactly the
reason `magent-state-hook` does: a 40-window attach starts 40 of these, and
booting the click CLI in each (the registration hub imports every command
module, then a config load) is the cost that once made a big attach take
minutes. That is also why the remote command is still a direct `psmux attach`
with the session picker only as a fallback.

*Why 255 is special.* OpenSSH reserves 255 for its own failures, and it is the
LOCAL client that reports it, so it is trustworthy on every OS and needs no
corroboration. It loops, forever, on a 2s-doubling ladder capped at 30s (an
all-night outage is then two handshakes a minute), with the ladder reset after
any connection that lasted 30s so a long-lived pane heals a blip in two seconds
rather than at the cap. This is the flaky-wi-fi hot path and it deliberately
costs no extra round-trip.

*Why exit 0 is NOT trusted, and what replaced it (revised 2026-08-17).* The
original table read exit 0 as "the user detached" and everything else as "the
remote command failed"; both stopped the pane. **Windows OpenSSH does not
propagate a remote command's exit status over a pty** — `ssh -t win-host
"exit 7"` reports 0 where POSIX sshd reports 7 — and a magent host is usually
Windows, because psmux is Windows-native. So a session that DIED on the host
handed the pane a 0 and the pane closed, announcing a detach the user never
asked for. Reported live: flaky wi-fi, forty windows gone, every one of them
claiming it was deliberate.

The fix is a second, out-of-band question. After any exit that is not 255 the
supervisor runs `ssh <target> "psmux -L <sid> has-session -t <sid>"` —
**without `-t`**, which is the whole trick: remote exit codes ARE truthful over
a non-pty channel on every OS. Alive means the client left while the work kept
running (a real detach, a quit picker, a killed ssh child) and the pane stops.
Anything else means keep dialling. Three details are load-bearing:

- *Only a positive rc 0 stops a pane.* A host with psmux missing from its sshd
  PATH answers 9009/127, an unreachable host answers 255, a timeout answers
  nothing — all of which keep the pane trying. Biasing every ambiguous answer
  toward "retry" is the direction the user asked for on a flaky link.
- *`-t <sid>` is mandatory* for the same reason `psmux.has_session` documents:
  a bare `has-session` exits 0 for a socket with no server (psmux keeps
  `__warm__` spares), which here would report every dead session as a
  deliberate detach — reintroducing the exact bug.
- *"Gone" is bounded, not infinite.* A host mid-reboot, or a 45-session
  `magent up` still working, genuinely answers "gone" for a minute and then
  "alive", so the pane retries; but a session the user really did `magent down`
  is never coming back, so after `SESSION_MISSING_MAX` consecutive gone answers
  the pane stops and says why rather than dialling a healthy sshd forever.

*Why not an in-band sentinel.* The obvious alternative — have the remote
command echo a marker on clean detach and scan the pane's output for it —
requires the supervisor to sit between ssh and the console. `_run_ssh`'s entire
contract is that it never does: the child inherits the real console handles, so
colors, mouse reporting and resize reach ssh untouched. Piping to read a
sentinel would cost every attach pane its interactivity to answer one question
a second connection answers for free. The probe also needs nothing new on the
host, so an old host works with a new client, and an old client (which never
probes) behaves exactly as it did.

*Why the real-ssh redial test dials an unroutable address* instead of asking a
remote command to exit 255: that stand-in silently became a no-op on Windows
and reported a green reconnect that never happened.

*Why the supervisor must carry the attach marker.* `cli/attach.py` decides a
pane is a corpse by scanning live process command lines for `-L <sid> attach`.
During a backoff sleep there is NO ssh process — so if the supervisor's own
command line did not carry that marker, `_sweep_dead_windows` would close the
window precisely while it was healing itself. `_spawn_windows` therefore passes
the remote command as the supervisor's `--remote` argument (rather than letting
it rebuild the command from `--session`), which puts the marker in the argv for
free, and `_CLIENT_PROCESS_NAMES` gained `magent-attach-client.exe`. Widening
that list can only ever make FEWER windows look dead, so the risky direction of
the corpse decision was not widened. The corpse machinery is NOT redundant
afterwards: it now answers "is anything driving this pane at all", which is
still "no" for a supervisor that failed to spawn, one the user Ctrl+C'd, one
that stopped on a failing remote command, and every pane from `--no-reconnect`
or an older magent.

*Not applied to `--no-mux`.* Without a multiplexer the agent is a child of the
ssh session, so a drop kills it; reconnecting would start a SECOND agent on a
conversation the user believes is still running. Reconnect is a psmux feature
because psmux is what makes the far side outlive the connection.

**An outage is a status line, not a log (2026-08-18).** Reconnecting correctly
turned out to be only half the job: a real wi-fi outage printed three lines per
attempt — our drop notice, our redial notice, and ssh's own `connect to host
... Connection timed out` — so ten minutes of flapping pushed thirty lines of
identical news through the pane the user was working in. The supervisor now
owns exactly one row while it is healing (`status_text` composes it,
`StatusLine` rewrites it with `\r\x1b[2K`), and the changing numbers live
inside it. Four decisions worth keeping:

- *The line is clipped to the terminal width, always.* This is the load-bearing
  one. A status line wider than the pane wraps, the next carriage return then
  lands on the wrap remnant instead of the line's start, and the "one row"
  becomes an unbounded scroll of half-lines — which is precisely the garbage
  the user reported seeing. `status_text` is pure and separate from the writer
  so that property is provable without a terminal, and it degrades in a
  deliberate order: the fixed `Ctrl+C` hint goes first, then the target (it is
  in the window title already), and the attempt/countdown go last.
- *ssh's own stderr is captured, not fought.* The noisiest lines come from the
  ssh CHILD, so no amount of repainting on our side can quiet them; only a pipe
  on fd 2 can. That is safe for two independent reasons, both verified rather
  than assumed: OpenSSH asks for passwords, passphrases and host-key
  confirmations through `read_passphrase()`, which opens the controlling
  terminal directly (`/dev/tty`, or the console on the Windows port) precisely
  so prompts survive redirection — so piping fd 2 cannot swallow a prompt; and
  the connection is made with `-t`, so the remote command's stdout AND stderr
  arrive multiplexed through the pty on our STDOUT, which stays inherited. Only
  ssh's own diagnostics land on the pipe, which is exactly what the `last: ...`
  clause reports. stdin and stdout are never redirected — the module stays a
  waiter, never a middleman. Two escape hatches keep the swallow honest: a
  changed host key is passed straight through (`STDERR_ALWAYS_SHOW`), and the
  captured tail is dumped verbatim when the pane gives up.
- *The "reconnected" record is written at the drop that ENDED the restored
  session, not the moment it came back.* At that moment ssh owns the console
  and the remote psmux has entered the alternate screen, so a line printed
  there lands inside the user's agent pane as garbage no redraw will repair.
  There is also no reliable establishment signal to print on: `ConnectTimeout`
  is 20s, so a child alive at t+2s is just as likely to be a hanging connect as
  a live session, and announcing on that guess would print a lie per attempt
  against a host that is down. Scrollback order is identical either way — the
  record still sits between the outage it ended and the next one.
- *Redirected panes and `--no-reconnect` get none of it.* `_stdout_is_tty` is
  checked once; without a tty there is no cursor animation (carriage returns in
  a log file are unreadable) and no stderr capture, so a piped pane keeps one
  plain line per attempt and ssh's errors keep landing on fd 2 where a log
  expects them. `--no-reconnect`'s promise is the historical bare-ssh pane down
  to which fd ssh writes on, so it opts out of both regardless of the tty.

**The status line owns the bottom row, and only the bottom row (2026-08-18).**
The first version of the above drew with a bare `\r\x1b[2K` — carriage return,
erase this row — at wherever the cursor happened to be. That turned out to be
the single worst place available. When ssh dies mid-session the terminal is
still in the ALTERNATE SCREEN: the remote psmux sent `\x1b[?1049h` on attach and
the process that would have sent the matching `l` is the one that just died. So
the pane keeps showing the agent's frozen last frame, with the cursor parked
exactly where that TUI left it — inside the prompt box, at the end of whatever
the user had typed and not yet sent. The reconnect warning erased their
sentence. Reported as "don't replace the text that is written in Claude Code,
because we may have text typed from before that we'd want to still send".

Every claim in that paragraph was measured under a real pty rather than reasoned
about (`tests/e2e/test_pty_attach_status.py`, which stages a real frozen frame
and replays the real byte stream through a small VT model in
`tests/e2e/_screen.py`, because a pty reports what a child WROTE and the
question is about what the terminal DREW). Four decisions:

- *There is no free row in a full-screen TUI, so stop looking for one.* The
  obvious fix is "own your own line": emit one `\n` to scroll a blank row into
  existence and repaint only there. On the normal screen that is free — the
  displaced row lands in scrollback. In the ALTERNATE screen there is no
  scrollback, so the scroll does not create a row, it DESTROYS the top one and
  shifts every remaining row up. That trades the bottom row (a hint line) for
  the top row (the oldest visible conversation) plus a whole-frame jump.
- *So the bottom row is taken deliberately, absolutely, and idempotently.* Every
  repaint is `\x1b7` + `\x1b[<rows>;1H` + `\x1b[2K` + text + `\x1b8`: save the
  cursor, jump to the last row, erase that row alone, put the cursor back. No
  newline is emitted for the whole of an outage, so nothing ever shifts. Because
  the address is absolute there is no per-outage "do we still own this row?"
  state — which matters, because after a reconnect the remote app repaints every
  row including ours and there is no signal that says so. The one-row cost is
  repaired by the remote's own redraw on reattach.
- *`\x1b[9999;1H` and a trust in CUP clamping is a trap — do not go back to it.*
  It is the standard "go to the last row without asking how tall the terminal
  is", and it fails on Windows: click's echo runs through colorama's
  ANSI-to-Win32 converter, which turns CUP into `SetConsoleCursorPosition` and
  silently DROPS a row outside the buffer. The cursor then never moves and the
  erase lands on the prompt after all — caught by the pty tier on its first run,
  invisible to every unit assertion. `_term_rows()` re-reads the real height on
  every repaint instead, which also follows a pane that is retiled mid-outage.
  The DECSC/DECRC cursor restore is best-effort for the same reason (colorama
  does not interpret those two, so a non-VT Windows console just leaves the
  caret on the status row); nothing depends on it, because the erase is absolute.
- *Leaving the alternate screen was considered and rejected.* `\x1b[?1049l`
  would hand back genuinely free real estate, but `1049` is defined to CLEAR the
  alternate buffer on the way out — the frozen frame the user asked to keep
  looking at would vanish for the whole outage — and its cursor restore is
  undefined when nothing ever saved one (the no-TUI remote command case).

**Keystrokes typed during an outage are forwarded, not eaten (2026-08-18).**
Measured, not assumed: the supervisor never reads stdin, so bytes typed while no
ssh child exists stay in the TERMINAL's own input buffer and are handed to the
next ssh child, which forwards them to the remote as if nothing had happened.
The user's "continue to type in the prompt section" therefore already works.
Pinned by `test_typing_during_an_outage_reaches_the_next_connection`, and worth
pinning because the tempting hardening — drain stdin so stray keys cannot echo
into the frozen frame — would throw away input the user meant to send. Two
honest caveats: the buffer belongs to the terminal, so a very long paste during
a very long outage can overflow it, and the terminal echoes those keystrokes at
wherever the cursor is, which the remote's redraw repairs on reattach.

**A psmux session must outlive the SSH connection that created it
(2026-08-17).** The premise the whole reconnect story rests on — "losing the
ssh client never loses work, because the session lives on the HOST" — was not
actually true on Windows. `magent attach` brings the host up by sending
`magent up` over SSH; Windows OpenSSH runs every session command inside a job
object marked kill-on-close, and job membership is inherited by every
descendant. `WindowsPlatform.launch_psmux_session` created each session with a
plain `Popen`, so the psmux SERVER it forked — and the agent that server would
host for the next eight hours — was born inside a job whose lifetime was the
laptop's wi-fi. Measured on a real host: 45 sessions decorated at 10:50, 16 at
11:03, with no magent process running in between, and the survivors were
exactly the sessions that had been created locally.

`procs.spawn_unjobbed` is the fix: `CREATE_BREAKAWAY_FROM_JOB`, falling back to
a plain spawn because CreateProcess fails outright when the parent job forbids
breakaway (and a bring-up must never raise). Three deliberate scoping choices:

- *It lives in `procs.py`, not `launch.py`.* The recipe already existed inside
  `launch.spawn_detached`, but `platform/windows.py` cannot import `launch`
  (launch imports platform). Two copies of a Windows process primitive is how
  one of them rots, so it moved down to the leaf and both callers reach it.
  `spawn_detached` keeps only its own half — the detached console.
- *It changes job membership and NOTHING else.* No console flags are added at
  the psmux call site, so the `new-session` child keeps inheriting the caller's
  console exactly as before; psmux allocates the session's pty itself and
  detaching the console would be a second, unrelated change to a spawn that
  works.
- *Only the creation spawn gets it.* `has-session`, `kill-server`, `send-keys`
  and the decoration `set`s are awaited inline and own nothing that must
  outlive anything, so they stay plain Popens.

**What this fix does NOT claim.** `TestSessionsOutliveTheirSshConnection`
(real-ssh, win32) kills the connection out from under a live session — both the
one that CREATED it and one ATTACHED to it. A control run with the breakaway
reverted to a plain `Popen` (PR #160) **passed either way** on
`windows-latest` with psmux 3.3.6: that runner's psmux already detaches its
server far enough to survive. So the job object is a real hazard the product
must not rely on luck to avoid — the escape costs one flag and the repo already
documented the mechanism — but it is **not a proven reproduction of the
reporter's 45→16**. The measured facts about that incident remain: 29 psmux
servers vanished between two `magent up` snapshots with no magent process
running in between, so something outside magent killed them.

The next instrument is already in place: the attached-client leg
(`test_a_session_survives_its_attached_client_dying`) tests the shape that
actually matches the incident — a flap kills every attach client at once, and a
server that followed its client would take exactly the attached sessions and
spare the rest. If that ever goes red, the cause is psmux-side and named.

**A resume flag with nothing to resume is dropped at COMMAND-BUILD time, never
retried at runtime (2026-08-11).** `claude --continue` — the registry default —
resumes the most recent conversation *for the current working directory*. In a
directory that never hosted one (a project just added to magent, a fresh
machine, a cleaned `~/.claude/projects`) claude prints "No conversation found to
continue" and exits, so the pane is a dead shell, the agent never starts, and
`revive` re-runs the same failing command forever. `sessions.build_start_command`
is the single function every command-build site routes through: it asks the
tool's registry entry (`AgentTool.fresh_command`) whether the configured command
carries an *implicit* resume flag and whether that directory has any stored
session, and drops the flag only when the answer is "yes, and no".

*Why not a shell fallback.* `claude --continue || claude` was the obvious fix
and is forbidden. It fires on ANY nonzero exit, so a mid-session crash, an auth
failure or a CLI regression would silently relaunch a FRESH agent — discarding a
live conversation and disguising a real defect as a working pane. It is also
unobservable: agent commands are delivered into psmux panes with `send-keys`, so
magent never sees the command's exit code and could not tell the two apart even
if it wanted to. The deterministic host-side probe (does
`<config dir>/projects/<encoded cwd>/` hold any `*.jsonl`) is the honest test,
and it is taken where the command is built.

*Which store answers is part of the question (2026-09-21).* claude keeps a
project's transcripts under the config directory the pane runs with, so
`~/.claude` is the right answer only for a pane that runs with no
`CLAUDE_CONFIG_DIR`. The probe therefore takes a `config_dir` — threaded through
`AgentTool.session_ids`/`fresh_command`, `build_start_command(...,
config_dir=)`, `launch._get_session_ids` and `psmux.eligible_projects(...,
config_dirs=)`, and resolved to `~/.claude` at CALL time when it is None, which
is every caller today. A probe that always read `~/.claude` would answer for a
store the pane never writes: it would drop `--continue` from a project that does
have a conversation on its own store, or keep it for one that does not — the
same dead-shell failure this decision exists to prevent, arrived at from the
other direction. Which store answers is a per-TOOL question, so the registry
asks the tool rather than resolving a path for it: `sessions/codex.py` accepts
the argument and ignores it, because `~/.codex` is one store per machine.

*Only a positive "no session here" rewrites anything.* An unknown tool, a tool
with no probe, an unresolvable directory, a command with no implicit-resume
flag, an explicitly named session (`--resume <id>`, `-r <id>`, or the bare
`--resume` picker), a per-window `command` override, and a probe that ERRORS all
keep the configured command byte-for-byte. A session file that exists but is
empty or corrupt counts as "a session exists" and keeps `--continue`: that
failure is a real defect the user needs to SEE in the pane. Every rewrite is
logged (`launch.log`, "no prior <tool> session in <dir>; starting fresh"), so
the decision is auditable after the fact.

*Where the probe runs matters.* The verdict is only valid on the machine that
will RUN the command, so callers pass None for a remote project rather than
consulting the local store: `launch.py` nulls `agent_dir` when `is_remote`, and
`psmux.eligible_projects` (which excludes remote projects outright) is the one
chokepoint feeding `bring_up`, `revive_sessions` and the `up --json`
`projects[].cmd` the attach client spawns no-mux windows from — all of which the
HOST computes over ssh, on the filesystem being probed.

*codex needs no special case.* Its resume form is the explicit
`codex resume <id>` subcommand, which magent only builds when it HAS an id, so
the default `codex` has nothing to rewrite. `codex_fresh_command` exists for
symmetry and handles the one hand-configured shape with the same hazard,
`codex resume --last`.

### Window titles are magent's, not the app's (2026-08-15)

The `magent:` title is not decoration. Four separate consumers resolve a window
*by* it — tiling's `magent-name` placement mode, `cli/attach.py`'s already-open
dedupe, the corpse scanner's window↔process pairing, and
`hotkey.py::project_from_title` — so a title rewritten out of the grammar does
not degrade one feature, it removes the window from the product. And every
program magent puts in a pane wants to write it: Claude Code emits OSC 0/2 title
escapes for its status, shells advertise their cwd, ssh names the host.

Two layers, in this order:

**1. The spawn-side lock — primary.** Every `wt` spawn passes
`--suppressApplicationTitle`, which tells Windows Terminal to ignore the tab
program's title entirely. This has been on all four spawn sites since the first
Windows backend, but it was hand-repeated with nothing enforcing it, so a fifth
spawn site could ship without it silently. It is now a lint rule (**MD006**,
`scripts/lint_rules.py`): a literal `wt` argv in `src/magent/` that lacks the
flag fails the gate. That rule is deliberately shallow — it reads the argv
*literal*, so the flag has to sit in the list next to the `"wt"` token rather
than be `args.append`-ed further down. That is the point: an append two branches
later is exactly the shape that loses the flag in a refactor and says nothing.
(Both Windows sites were appending; they now carry it in the literal.)

*POSIX is a mixed bag, honestly.* `--title` is only an INITIAL title on most X11
emulators, so each backend takes the strongest lever it actually has: kitty's
`--title` permanently fixes the OS window title (so it already is a lock),
alacritty gets `-o window.dynamic_title=false`, xterm gets
`-xrm XTerm*allowTitleOps:false`. gnome-terminal, konsole, Terminal.app and
iTerm expose no per-launch equivalent — see the known-debt ledger.

**2. Reassertion — the repair, Windows only.** The lock cannot be universal (no
lever on some emulators; a window can be adopted from a spawn magent did not
make), and a stomped title is otherwise *permanent*: `parse_title` stops
recognizing it, so nothing in the product can find that window again — including
the code that would fix it. `BadgeRenderer` (attention daemon) therefore
remembers each window it has resolved **by handle**, an identity that survives a
title rewrite, and retitles a remembered handle whose title stops parsing. It
rides the `snapshot_windows()` pass that already runs every tick — no new poller,
and still zero writes on a quiet tick.

*Why the repair is narrowly gated.* It only fires when the remembered name still
has a live session in the agent-state store, and both bookkeeping maps are pruned
to the live window set every tick. The failure mode being bought off is handle
recycling: stamping `magent:<name>` onto a stranger's window would not merely
mislabel it, it would get that window **tiled**. A missing badge is a worse-
looking bug and a much cheaper one.

### The Alt+V listener is supervised, not spawned once (2026-08-15)

The listener used to be a ONE-SHOT spawn: whichever `magent --go` or `magent
attach` ran last called `start_hotkey_listener`, and after that nothing in the
product ever looked at it again. Observed live: a listener last started eight
days and one reboot earlier, upload server still running, `magent status`
reporting `Alt+V listener   off  (starts with 'magent attach')` and exiting 0.
Two failures at once — the hotkey was dead, and the tool said that was normal.

**Owner: `serve`.** The upload server is the long-lived process the Alt+V chain
already posts into, so "serve is up" and "Alt+V works" collapse into one fact.
`upload_server._supervise_hotkey` runs on a daemon thread off `run_server`,
checks immediately and then every `HOTKEY_SUPERVISE_INTERVAL_S` (30s), and
delegates to `launch.ensure_hotkey_listener`. Every failure is a log line and
another try next interval: supervision must never take down the thing actually
serving uploads.

*Why a second entry point.* `ensure_hotkey_listener` is NOT
`start_hotkey_listener`. The launch/attach paths are the *wiring* callers — they
know which target the listener should serve and deliberately re-aim it when that
changes, which is what `hotkey_restart_reason`'s "target change" branches are
for. A supervisor knows no such thing: `magent attach` points the listener at a
REMOTE host so F2 opens projects over VS Code Remote-SSH, and a supervisor that
re-applied its own loopback URL every 30 seconds would fight attach forever —
killing the remote-wired listener on every pass and permanently breaking F2 on
remote fleets. So `ensure_hotkey_listener` re-checks a live listener against
**its own manifest target** (`supervised_hotkey_target`) and only chooses a
target for a listener that is not there. Version skew still restarts it, in
place, on its own target.

*The listener is deliberately NOT stopped with serve.* `down --all` already
stops both — server first, listener second, so the supervisor is gone before the
listener is and cannot resurrect it — and a user restarting serve should not
lose their hotkey in between.

*Never two listeners.* Unchanged: the pid-file + manifest dedupe inside
`start_hotkey_listener` is what guarantees it, and every caller still routes
through it. `exclusive_lock("hotkey-supervisor")` is taken by the supervisor
alone (two serve daemons on different ports would otherwise both spawn) and
deliberately NOT by launch/attach, so an interactive attach re-aiming the
listener can never be blocked by a background thread.

**`MAGENT_HOTKEY_SUPERVISOR` (default on) is a test-isolation requirement, not a
preference knob.** The listener installs a SYSTEM-WIDE low-level keyboard hook,
which no HOME redirect can contain — so without an opt-out, every tier that
starts a real `magent serve` (e2e, soak, dist, browser, and a plain
`pytest tests/e2e/` on a developer's own Windows box) would install one on the
machine running the tests. Every such fixture sets it to `0`; the `interaction`
tier sets it to `0` because it spawns the listener itself, and its new
supervision test sets it back to `1` as the behavior under test. It doubles as
the escape hatch for a user who wants to own the listener's lifetime.

**Observability.** `cli/status.py::_listener_state` gained a fourth state,
`dead` — no listener, on a hotkey-capable platform, while the upload server is
*serving* **and permitted to supervise**. It is red, carries
`LISTENER_REPAIR_HINT`, and degrades the exit code to 3, consistent with the
documented contract. `off` is reserved for the cases where nobody promised a
listener, and its hint says which one: no hotkey support, no server, or
supervision opted out. Two exclusions matter, and both are "do not invent a
promise": a *dead* upload server does not also report a dead listener (the
upload line already says so, and a second red line for the downstream symptom is
noise), and neither does a server whose owner set `MAGENT_HOTKEY_SUPERVISOR=0`.
`doctor`'s `hotkey` check imports the same state machine rather than reimplement
it, so the two surfaces cannot disagree about whether Alt+V works.

**The repair hint never goes through `down`.** `LISTENER_REPAIR_HINT` once said
"magent down --all, then magent serve", which kills the agent in every
configured session to revive a keyboard hook (pinned by
`tests/unit/test_status.py`: the hint never contains `down`). The hint now
follows from what the supervisor does, below: serve repairs a dead *or* wedged
listener itself, so the only manual step left is serve not running
(`magent serve --ensure`). `hotkey_cmd`'s "already running" line no longer says
"stop it with `magent down --all`" either.

**A wedged listener is replaced, not just reported.** A listener whose pid is
alive but whose heartbeat stopped used to be permanent: `ensure_hotkey_listener`
only restarted on a version/target mismatch, so the manifest still matched and
`status` stayed red until a human ended the pid. `_supervise_hotkey` now carries
one `launch.ListenerWatch` and `ensure_hotkey_listener(url, watch=watch)` calls
`retire_wedged_listener` before its usual start. The design decisions, each of
which is a way the replacement could otherwise hurt someone:

- *The proof is four facts, not "the heartbeat is old".* The heartbeat file
  exists and has been silent longer than `WEDGED_LISTENER_GRACE_S` (3x the
  `status` stale threshold = 90s: "stale" is a label, this verdict ends a
  process); the pid's identity is readable; its image is python; and it was
  **created no later than the last pulse**. The last one is what survives a
  python-on-python pid reuse -- a process born after the final heartbeat cannot
  be the one that wrote it. A missing heartbeat is never a wedge (there is
  nothing to compare a creation time against).
- *Two ticks.* The same pid must show the same last pulse on two consecutive
  supervisor ticks. A machine that slept looks wedged on the tick it wakes and
  healthy on the next; the confirm turns that into a non-event.
- *The kill is identity-verified.* `procs.terminate_verified` re-reads (image,
  creation time) through the very handle it terminates with, so a pid recycled
  between the proof and the kill is never hit. It is not `stop_listener`'s
  `taskkill /PID /F`, which trusts the pid file blindly. `hotkey.forget_listener`
  then drops the pid file, manifest and heartbeat without signalling anything
  (a pid file can still read live for the instant before Windows finishes
  tearing the process down, which is exactly when the replacement spawns).
- *Replacement is not re-aiming.* The target is read from the manifest BEFORE the
  kill (forgetting the listener clears the manifest) and the fresh listener is
  started at exactly that target, so `magent attach`'s remote wiring survives.
  Callers with no `watch` (the one-shot launch/attach paths) can never end
  anything.
- *Bounded.* `WEDGED_REPLACE_COOLDOWN_S` (300s) caps a listener that wedges the
  instant it starts at 12 replacements an hour, and a kill that is refused
  still starts the cooldown so it is not retried every tick. Each outcome is a
  `hotkey.log` warning (replacing / could not end / cooldown -- the last once
  per episode). `MAGENT_HOTKEY_SUPERVISOR=0` returns before the watch exists.
- *It sees only what the other daemon seams let it see.* The pid comes from
  `hotkey.listener_pid`, which discards a pid file written before the last boot,
  so after a reboot a recycled pid is never even a candidate -- the identity
  proof is the second line of defense, not the first. And
  `ensure_hotkey_listener` asks `launch.session0_block` before it reads
  anything: in Session 0 the start is refused, so a replacement there would end
  the desktop's listener and put nothing back. Both pinned in
  `tests/unit/test_daemon_lifecycle_seams.py`.

Not covered, on purpose: a listener whose identity cannot be read (e.g. it runs
elevated) is left alone with one warning per pid, and a heartbeat that pulses
from a wedged *hook* (the message loop turns, Alt+V is dead) is invisible to
this check -- the heartbeat proves the loop, not the chord.

**Per-press feedback.** Every Alt+V press now ends in exactly one
`ALTV outcome=<x> project=<y>` line in `hotkey.log` (closed vocabulary,
`hotkey.ALTV_OUTCOMES`), and every failure also reaches the screen through the
`/api/flash` psmux status line F2 already used — no new notification subsystem.
The one deliberate silence is `not-a-magent-window`: Alt+V outside a magent
window is another app's chord, not a failure, so it is DEBUG-only (at INFO it
would log every Alt+V the user ever presses). `no-image` still passes the chord
through — the pane may want a plain Alt+V — but says why nothing was uploaded.

### The upload server is supervised too, and by the attention daemon (2026-08-19)

The listener decision above ends one level short. `serve` supervises the
listener, sessions get revived, attach panes redial — and nothing at all watched
`serve` itself, the process every mobile upload and every Alt+V press goes
through. On 2026-08-18 it died silently twice on the same live host: once inside
a machine-wide ConPTY wedge, once unexplained between ~13:00 and ~16:10. Both
times the first symptom was the owner pressing Alt+V and getting nothing, hours
after the fact, and the machine had almost nothing to say about it afterwards.

Two separate defects, fixed separately.

**1. A death that leaves a trace.** `run_server`'s `try/finally` logged the same
`stopped` for a Ctrl+C and for a crash, and a detached serve has no console for a
traceback to reach — so even the logfile could not distinguish "the user stopped
it" from "it fell over". Every exit now names its reason (`stopped: keyboard
interrupt` / `stopped: crashed` / `stopped: loop returned`), a crash is logged at
**exception level before it propagates** (which is also what hands it to Sentry —
errors-only, logging integration at ERROR), and the fatal "no bindable address"
startup failure gets an ERROR line of its own rather than only an exception into
the void. Nothing is swallowed: both handlers re-raise, and the CLI still exits
non-zero. The secondary (Tailscale) bind's `serve_forever` runs through
`_serve_bind`, which logs its own death and does **not** re-raise — that thread
dying must not take the loopback bind with it, but it must not be silent either.

**2. Owner: the attention daemon.** `serve` cannot supervise itself; a supervisor
that only ran while serve ran would supervise nothing the moment serve died. The
attention daemon is the other long-lived process, it already polls on an
interval, and it is the one users leave running — so
`cli/attention_cmd._upload_watchdog` hands `run_attention_loop` an `on_tick` hook
driving `launch.UploadServerSupervisor`.

*Where the seam lives, and why it moved.* Everything — the loopback probe, the
argv builder, the detached spawn — is in `launch.py`, next to `spawn_detached`
and `ensure_hotkey_listener`. It used to live in `cli/background.py`, and a src
module cannot import the cli package (LS-A-001: `cli/__init__` imports every
command module for registration, so a reverse import cycles). Rather than write a
second spawn recipe, `cli/background._maybe_start_upload_server` became a thin
delegation to `launch.ensure_upload_server`, so the server the watchdog revives
is byte-for-byte the one the launch path starts.

*Detection is cheap, respawning is not.* The probe rides the existing poll tick
(one refused loopback connect; no second timer, no new thread), while the
**respawn rate** is bounded by `UPLOAD_RESPAWN_COOLDOWN_S` (60s, overridable via
`MAGENT_UPLOAD_RESPAWN_COOLDOWN_S`). Splitting the two is the whole design: a
serve that dies at 03:00 must not wait out a long timer before anyone notices,
and a serve that crashes on startup must not be respawned in a tight loop.

*The pid is diagnostic, never decisive.* Liveness is the TCP probe alone. The
recorded pid is read only for the log line, deliberately: `run_server` writes its
pid file **after** the bind, so the pid can never be the earlier signal, and a pid
number the OS later recycles onto an unrelated process would blind the watchdog
permanently. What it buys is a truthful diagnosis — "recorded pid 8123 is gone"
(the observed failure) reads very differently from "pid 8123 is alive but not
answering", which is a wedge, not a death.

*Two gates, answering different questions.* `settings.uploadServer` is the
config's own switch: a user who turned the upload server off is not
second-guessed, and nothing is resurrected on a machine that never had one.
`MAGENT_UPLOAD_SUPERVISOR` (default on) is the runtime opt-out for somebody who
runs serve under their own supervisor — and, exactly like
`MAGENT_HOTKEY_SUPERVISOR`, it is a **test-isolation requirement**: an
`attention -d` fixture that quietly spawned a real `magent serve` on a runner
would leak a process no teardown knows the pid of. Every fixture that starts a
real daemon sets it to `0`; the `e2e` watchdog tier sets it to `1` as the
behavior under test.

*Observability.* `status` prints one `Repair:` line — `magent attention -d` —
when the upload server reads DEAD **and** the daemon is off **and** it would
actually supervise (config on, env not opted out). Suggesting the daemon to
someone who disabled it would be advice that does nothing.

### The attention daemon is supervised by serve, and a restart is not a crash (2026-09-30)

The decision above left one gap. The attention daemon supervised `serve`, but
nothing supervised the daemon. On 2026-09-29 the owner restarted Windows. The
next bring-up (`_start_psmux_and_upload`, the `--go`/menu launch path) started
`serve`, and serve started the Alt+V listener. Nothing restarted the daemon.
`status` then printed `CRASHED (daemon died — see logs)` about a daemon that had
never crashed: the restart killed it before it could remove its heartbeat, and
a leftover heartbeat was the crash marker.

**1. Owner: `serve`.** The choice was between the bring-up path ensuring the
daemon and serve's own supervisor thread ensuring it. Serve won, for two
reasons.

- The bring-up only runs when somebody launches. A daemon that crashes at 03:00
  would stay dead until the next `--go`. That is the failure the upload-server
  supervisor already fixed for serve.
- Serve is the process that is effectively always up, and every entry point
  already starts it: `--go`, the menu, `up` and `attach` (`serve --ensure`).

So the supervision is now mutual, and each half covers the moment the other is
dead. Serve runs a watchdog (`cli/attention_cmd.AttentionDaemonSupervisor`)
that looks as soon as it binds and then every `WATCHDOG_INTERVAL_S` (30s). The
first look is immediate because a serve that starts right after a reboot is
exactly when the daemon is missing.

*It revives only what was running.* The heartbeat file answers "was it
running". Every clean stop removes it (`attention --stop`, `down --all`,
Ctrl+C). A crash, a kill or a restart leaves it. So serve starts a daemon only
when no daemon is live and a heartbeat lingers. A daemon the user never
started, or stopped on purpose, stays stopped. A background process overruling
a foreground decision would be the same kind of surprise as a watchdog nobody
asked for. An explicit `settings.attention` autostart key was considered and
left out: the heartbeat already records the user's intent, and there is no
config switch whose absence means "yes".

*It revives through the front door.* The revive runs the same `attention -d` a
human types. Its `exclusive_lock("attention")` and live-pid check are what
guarantee one daemon, so two serves on two ports, or a serve racing a human,
end with one daemon and a "launch already in progress". It also skips a config
the daemon would refuse. A config it cannot read, or one whose renderers are
all off or unsupported, gets one log line and no spawn, rather than a spawn
every cooldown that exits 1 forever.

*The cooldown outlasts the launch.* `ATTENTION_RESPAWN_COOLDOWN_S` is 60s, and
a test pins it above `procs.REGISTRATION_TIMEOUT_S`. The launcher holds the
lock only until its child registers or that window runs out. A cooldown shorter
than the window could start a second launcher beside a child that is merely
slow ("A slow child is not a failed child"), and a late registration would
leave two daemons.

*Where the code lives.* `upload_server.run_server` takes generic
`watchdogs` hooks and runs each on a daemon thread through `_run_watchdog`,
after the bind. A serve that lost the port exits `PortInUse` having started
nothing. The attention-specific judgement (pid, heartbeat, renderer plan)
lives in `cli/attention_cmd.py` next to `_upload_watchdog`, so both halves of
the mutual supervision sit in one file. `serve_cmd` builds the hook and hands
it down, because a src module must not import the cli package (LS-A-001).

*Two gates.* `MAGENT_ATTENTION_SUPERVISOR=0` is the opt-out, and like its three
siblings it is a **test-isolation law**. A test that starts a real serve would
otherwise start a real daemon behind it the moment its home held a heartbeat,
and that daemon would badge the developer's windows with a pid no teardown
knows. `tests/conftest.py` pins it to 0 for every tier, and every fixture that
builds a child `env=` sets it next to `MAGENT_UPLOAD_SUPERVISOR`. The second
gate is the Session-0 seam every daemon spawn passes, `launch.session0_block`
(below). A serve in logon Session 0 (a foreground `magent serve` over ssh)
would start a daemon on a desktop nobody sees, holding the `attention.pid` the
real desktop's daemon needs, so serve supervises only where a launch would
"run". `attention_watchdog` asks it before building the supervisor, and
`AttentionDaemonSupervisor.tick` asks it again before every spawn (once per
cooldown in the log), so no caller can route around it.

**2. A restart is not a crash.** `procs.boot_time()` answers when the machine
last started. It uses `GetTickCount64` on Windows, `sysctl kern.boottime` on
macOS and `/proc/stat` `btime` elsewhere, all inside the `procs` leaf, so no
business logic branches on `sys.platform`. `status` now has five attention
states:

- `crashed` is a lingering heartbeat from after the boot. It stays red and
  exits 3.
- `off-since-restart` is a lingering heartbeat whose last pulse predates the
  boot. It prints "not running since the last restart" in yellow and exits 0.
- `off` means no heartbeat. `on` and `stale` are unchanged.

`off-since-restart` exits 0 because nothing failed. It is the same fact as
`off`, plus the reason. Exit 3 means "something that should be running broke",
and a monitor paging on every reboot would teach people to ignore it. The hint
says what will actually happen: "the upload server restarts it" when a serving,
supervising serve is up, otherwise the command to run. An unknown boot time
keeps the old verdict. `doctor`'s `attention` check reads the same state
machine and is WARN at worst.

The same boot time closes a pid-reuse hole. `attention_cmd.daemon_pid()` treats
a pid file written before the boot as stale, whatever that pid is doing now. A
restart leaves the file behind and Windows hands pid numbers out again, and a
recycled pid would read as a live daemon: `status` would report ON and serve
would never revive the real one.

The same hole existed for the other two pid files, and they are read by more
than a status line.

- **The listener's pid file.** `hotkey.listener_pid()` is the one reader behind
  every listener decision. With a recycled pid, serve's supervisor kept it and
  never started a listener, so Alt+V stayed dead after the reboot. `status` and
  `doctor` called it STALE and blamed a wedged message loop.
- **The upload server's pid file.** `upload_server.server_pid()` feeds three
  decisions: `status` choosing between DEAD and off, `stop_server` choosing what
  to taskkill, and the phone-URL port pick.

Both readers now clear a pre-boot pid file and read None, so a `down --all`
after a reboot can no longer kill an unrelated process that was handed the old
number. The attention watchdog's own read of the serve pid stays log-only, and
a test pins that. It decides on the port probe alone, so a live pid on record
never stands in for a server that does not answer.

"Before the boot" allows 30s of slack (`procs.BOOT_CLOCK_SLACK_S`), because the
boot time is derived rather than recorded. Windows computes it as "now minus
uptime", so a clock correction after the boot moves it, and Linux's btime is
rounded. The two errors are not the same size. A live listener's pid file read
as pre-boot would be discarded, and serve would start a second listener beside
it: two keyboard hooks, and every Alt+V pasted twice. A heartbeat from just
before a very fast restart that reads as newer than the boot only gets the old
wording.

*The teardown order follows the supervision edges.* `down --all` used to stop
serve, then the listener, then the attention daemon, so for a moment the daemon
outlived the serve it supervises. It could start a new serve in that moment,
and that serve would then restart the listener. The order is now supervisor
first, all the way down: the attention daemon, then serve, then the listener.
The one edge that points back up is serve reviving attention, and it fires only
while the daemon's heartbeat lingers. `stop_daemon` withdraws the heartbeat
BEFORE the kill (and again after), so a deliberate stop is never mistaken for a
crash in the gap between the kill and the cleanup. That alone left a window:
the daemon's heartbeat thread pulses until the kill lands, so a pulse can slip
in after the withdrawal, and a serve tick between the kill and the second clear
saw exactly a crash. So the supervisor never acts on a heartbeat that is still
fresh by `status`'s own window (`log.HEARTBEAT_MAX_AGE`, 30s). The same rule
covers a daemon running in Session 0: this desktop cannot open its pid, so
`daemon_pid()` is None while it keeps pulsing the shared heartbeat, and a
revive there would be a second daemon. A real crash stops pulsing and is
revived once the pulse goes stale, a tick or two later. A restart's heartbeat
is almost always stale by the time serve is back up, so it is revived at the
first look. `magent down --server`
without `--all` still leaves a running daemon, which brings serve back within
its cooldown, as it has since the upload-server supervisor landed.

*Known gap.* Windows Fast Startup ("Shut down" with hybrid boot) resumes a
hibernated kernel, so the uptime counter does not reset and the boot time is
the last cold boot. A daemon killed by a Fast-Startup shutdown still reads
`crashed`, which is the old behaviour, and serve still revives it, because
revival does not depend on the boot time. A real Restart always cold-boots.

### One port, one server (2026-09-26)

The watchdog above, `serve --ensure` and a hand-run `magent serve` can each
start a server while another is still starting, and the design assumed the loser
would fail its bind. On Windows it did not. `ThreadingHTTPServer` sets
`SO_REUSEADDR`, and on Windows that option lets a second process bind a port
that is already **listening**. Measured: two live servers on one port, both
logging `listening ... :15505`, with the pid file naming only the later one. The
watchdog then killed or revived the wrong server, and `/health` was answered by
whichever one the kernel picked. Linux `SO_REUSEADDR` never allowed two live
listeners on the same address, so only Windows ever showed it. (BSD/macOS let a
specific address coexist with a listening wildcard; not addressed here.)

`_NoFqdnHTTPServer` now owns its bind options (`_claim_port_options`), set
before the bind. On Windows it sets `SO_EXCLUSIVEADDRUSE` and not
`SO_REUSEADDR`, so a second serve is refused. Exclusivity is what refuses a
`serve --host 0.0.0.0` against a held loopback port. It does not stop a foreign
program from binding the wildcard over a loopback holder, and a same-address
`SO_REUSEADDR` socket was refused on this Windows build even before. On POSIX
it keeps `SO_REUSEADDR`, which on Linux only lets a restart rebind past the
previous server's `TIME_WAIT` connections. Windows never held a port hostage to
`TIME_WAIT` (measured with ~20 such connections on the port, and again with
FIN_WAIT_2, a still-ESTABLISHED accepted connection, and a killed server
process), so an upgrade still restarts serve at once. The branch is a
`sys.platform` check, not a capability probe: it is socket semantics, not a
feature.

A bind refused because the port is held raises `PortInUse`, not the old generic
error. `EADDRINUSE` always means held. Windows' `WSAEACCES` means one of two
things. It can be an exclusive wildcard holder refusing a specific address, or
a port Windows has reserved (a Hyper-V / WSL / Docker excluded range, measured
at 127.0.0.1:17000). A 0.3s connect tells them apart (`_holder_answers`).

- **Something answers:** a holder, so `PortInUse`.
- **Nothing answers:** a reservation. There is no first server to defer to, so
  it degrades like any unbindable address. If it was the only address, the fatal
  "no bindable address" ERROR (what Sentry captures) names the reservation and
  points at `netsh int ipv4 show excludedportrange protocol=tcp`.

Held on **any** of serve's addresses counts: serving only the free ones would
be two servers and one pid file again. Whatever was already bound is closed,
and the pid file is untouched, because ours is only written after the bind. The
trade-off is on record: a foreign program holding only the Tailscale address
now keeps loopback down too, where it used to degrade, and the watchdog retries
at its cooldown pace. `PortInUse` is logged at WARNING, not ERROR. A watchdog
or `--ensure` spawn that loses the race is **supposed** to end here, so it is
not a crash for Sentry. The CLI shell prints the reason and exits 1 instead of
a traceback. An address that cannot be bound for any other reason (a Tailscale
IP that went away) still degrades with a warning, as before.

One spawner never asked first. Every `--go` and menu bring-up spawned
`magent serve` without looking. On a machine whose server was already up, that
second serve could only die of `PortInUse`: a WARNING in `upload.log` on every
bring-up, which read like a duplicate-server fault. It now goes through
`ensure_upload_server`, the same probe-then-spawn every other spawner uses.

Audited and left as they are:
- **Two addresses at once.** Two serves on the default addresses cannot both
  bind, because loopback is always claimed first and is exclusive. Two
  explicit, disjoint `--host` addresses are two servers by the user's choice.
- **Session 0 does not change the answer.** Sockets are machine-wide, so a
  Session-0 serve conflicts with the desktop's like any other process.
- **The 0.3s port probe can say "free" wrongly.** It happens when a wedged
  serve's backlog is full, or when a server is bound only to a non-loopback
  address. The spawn it lets through dies of `PortInUse` at the watchdog's
  cooldown pace, so it cannot produce a second server.

Pins:
- `tests/unit/test_upload_server.py::TestOnePortOneServer` (real sockets,
  current OS, including the set-before-bind order);
- `TestClaimPortOptions` / `TestPortTaken` (both OSes, fake socket);
- `TestHolderAnswers`;
- `TestRunServerOnAHeldPort` (held, reserved, and reserved-secondary);
- `tests/e2e/test_real_upload.py::test_a_second_serve_on_the_same_port_exits_and_leaves_the_first_alone`.

### One liveness enumeration, and a shutdown that verifies (2026-08-18)

Reported twice on a live 46-session Windows host: after `magent down --all`, a
fixed set of sessions "stay always" — and they were always the TAIL of the
config, in config order. Two contradictory data points came with it. On the
17th, `status` said 30 running / 15 stopped and the `down --all` a moment later
named only the last 16, five of which `status` had just called *not* running.
On the 18th, `down --all` said "Stopped 46" while the laptop's picker still
listed the last 11 as alive and attachable.

Three defects, each independently sufficient to produce that.

**"Which sessions are live" had three answers.** `psmux.psmux_status` (behind
`status`, `down`, the menu), `session_picker._live_sessions` (the picker) and
`psmux.discover_sessions` (the upload server) each ran their own
`has-session -t` sweep with a different retry policy. Only the picker retried
its misses — with a comment saying, correctly, that probes flap under the load
of many running agents and that *a dropped probe silently hides a live
session*. So the picker could be attached to a session `status` called stopped
and `down` therefore never touched. Now there is one function,
`psmux.live_sessions`, and all three call it; the bring-up creation verify
(`_missing_sessions`) stays separate on purpose and says why in its docstring.

**`down --all` acted on a probe result, not on a promise.** Its own help says
"Stop EVERY psmux session", and it was implemented as "stop whatever that
single fan-out happened to return" — so a session the probe missed was neither
stopped nor mentioned. `down` now kills every *configured* eligible session in
scope. `kill-server` against a socket with no server is a harmless no-op, so
over-targeting costs one wasted subprocess while under-targeting costs the
whole feature. The local-vs-remote decision still keys off the LIVE local
sessions, because "nothing is running here" is what tells an attach client to
act on the remembered host.

**`down` reported the loop it ran, not the world it changed.** `kill_servers`
discarded every `kill_server` return value and answered with the full list of
names it had attempted; the command printed that length. With psmux 3.3.6
exiting 0 for kills that do not take, honouring the rc would not have been
enough either. `psmux.stop_sessions` is the answer: probe → kill → settle →
re-probe → kill the survivors again → re-probe, returning
`(stopped, still_running)`. `down` and the menu now print only what was proved
stopped and name any survivor in red. A survivor is also an ERROR in
`launch.log`.

Two contributing timeouts went with it: `kill_server` is now bounded (a wedged
psmux server answers nothing, and one stuck socket must not hold a 46-session
shutdown hostage), the kill is a bounded fan-out rather than a sequential
sweep, and `cli/attach.py::_REMOTE_DOWN_TIMEOUT_S` went 60s → 300s. That last
one was itself a tail-truncation mechanism: 46 sockets could outrun a 60s SSH
budget, ssh was killed mid-shutdown, and what survived was exactly the part of
the config the sweep had not reached — a config-order tail.

### A press narrates itself, and nothing on that path may block it (2026-08-18)

Reported as two complaints about one feature: "Alt+V is working but the status
isn't showing", and "the status always shows up pretty late". Both were real,
and neither had the cause the architecture suggested.

**Why nothing showed.** `psmux.flash_message` bounded its `display-message`
subprocess at 3 seconds — and `subprocess.run(timeout=...)` does not merely
stop waiting, it KILLS the child. On an idle socket that command costs 60-130
ms (measured against this machine's live sessions), but under real load — 46
live sessions, a discovery fan-out, a spawn storm, all competing for Cygwin
process creation — it routinely ran past 3 s. The production log is unambiguous:
every flash in one Alt+V burst reads `status-line flash failed for
project=<p>: Command '[...display-message...]' timed out after 3 seconds`. The
product was throwing its own feedback away, on purpose, exactly when the
machine was busy enough for the user to want it. The bound is now 20 s
(`psmux.FLASH_TIMEOUT_S`): the wait happens on an HTTP handler thread, never on
a press, and waiting is what keeps a project's messages in order.

**Why it was late.** The earliest feedback a press could produce was the
server's "uploading" flash — which fired only after the clipboard read, the
BMP wrap, the whole multipart POST, and (on a cold 10 s cache) a full
`discover_sessions` fan-out inline in the request handler: 438-861 ms on this
machine's 46 sessions when idle. And success flashed nothing at all from the
listener, by an earlier deliberate decision ("the server drives the progress
line on the happy path") that left a working Alt+V indistinguishable from a
dead listener.

**The shape of the fix.** The press pipeline moved out of `hotkey.py` into
`altv.py`, and narrates itself: `capturing...` is dispatched as the FIRST
statement of `handle_press`, before the clipboard is touched; `uploading...`
brackets the POST; the outcome — success included — replaces it. Measured end
to end (real serve, real subprocess spawn, ~900 KB image): **65-176 ms from the
press to the acknowledgement on the bar**, versus a first message that
previously arrived after the whole upload if it arrived at all.

Three constraints hold it together, each with a test that fails if it is
undone:

* **Async, so a press never waits on its own progress report.** A status-line
  write has been measured in seconds; three synchronous phases would put that
  on the critical path of the paste.
* **ONE pump, so the phases stay in order.** Three fire-and-forget threads
  would race, and an "image sent" that overtakes an "uploading..." leaves the
  bar lying. The pump is FIFO, waits for each flash to land before sending the
  next (`/api/flash` answers only once psmux has the message — that reply IS
  the pacing signal), and cannot die: a pump that ends on one bad message
  strands every message queued behind it.
* **One bar, one narrator.** `upload_server` no longer flashes for uploads
  carrying `?project=` (the listener's marker). Two writers on a one-line bar
  can only race, and the loser would be the specific message — the server's
  text is generic by construction, the listener's says *which* failure it was.
  Mobile uploads, whose sender is looking at a phone, keep the server's flash.

The outcome vocabulary grew to carry that specificity (`serve-unreachable` and
`inject-failed` split out of what was one `upload-rejected`), and every member
except the pass-through has a status-bar reason in `altv.OUTCOME_REASONS`; a
test asserts no two outcomes share a sentence, because a collapsed vocabulary
is precisely how "it failed" came back.

Two things this also bought, both cheap: `/api/flash` logs every message it
serves (`flash project=… msg=…`), so "the status isn't showing" is answerable
from `upload.log` after the fact rather than only by reproducing it; and the
phase messages carry their own linger time (`ms=`), because a phase that
expires mid-upload leaves a blank bar that reads exactly like the silence the
channel exists to end.

**What was disproven along the way,** recorded so it is not re-suspected: psmux
3.3.6 (a Cygwin tmux 3.3.6 build) repaints the status bar IMMEDIATELY on
`display-message` — measured on a throwaway socket under a real ConPTY client
at 60-100 ms from command issue, with a later message replacing a live one just
as fast, and with no difference between the `-t` and target-less forms for the
DISPLAY (non-`-p`) case. `set -g status-left` repaints immediately too. There
is no `status-interval` tick to wait for and no `refresh-client` to add; the
latency was never in the multiplexer.

### The upload reply is not hostage to the paste (2026-08-18)

The narration above was honest about everything except its own worst case. A
press on this machine took 60-75 seconds and ended in "Alt+V: upload failed -
is magent serve running?" — while the SAME upload is logged completing
`ok=True injected=True` 74 s after the press. Nothing had failed. The user was
told their screenshot was gone, about a file already sitting in
`~/.magent/uploads`.

Three facts composed into that lie:

* `psmux.send_keys` was the **only** psmux call in that module with no
  subprocess timeout at all — and the one an HTTP request handler ran inline,
  before replying. Every other probe here (`pane_cwd`, `capture_pane`,
  `flash_message`, `kill_server`) had been bounded already; this one was
  missed because it is the only one whose result the *product* wanted rather
  than a diagnostic.
* A psmux control command against a session whose attached terminal is busy or
  unfocused has been measured from 3 s to past 70 s. It is not a rare stall; it
  is the same load that made every status-line flash in months of `upload.log`
  time out.
* The listener bounded its POST at 20 s. Server unbounded + client bounded is a
  guaranteed false negative under exactly the load the feature is used in.

**The fix keeps the paste, and stops waiting for it.** `send_keys` takes a
`timeout` (default `SEND_KEYS_TIMEOUT_S`, 20 s) and degrades to `False` with a
WARNING instead of hanging or raising. `upload_server._inject_paste` then runs
the paste on its own thread and waits `INJECT_GRACE_S` (3 s) for it: the
overwhelmingly common fast paste is still reported as the plain
`injected: true` it is, and a stalled one is answered early and honestly. The
reply grew a third state — `inject_pending` — because `injected: false` alone
cannot tell "psmux refused" from "psmux has not answered yet", and collapsing
those two is precisely what rendered as a failure. `altv` reads all three:
`ok` / `inject-pending` / `inject-failed`, with `inject-pending` carrying
"image saved - psmux is slow, paste still pending" **in the healthy tint** —
red on that bar reads as "your screenshot is gone".

Measured against a real serve and a real multiplexer binary that sits on
`send-keys` for 30 s (`tests/e2e/test_altv_flash.py`, the `stalled_paste_fleet`
tier): **3.1 s from the press to the outcome on the bar**, versus 20 s and a
false failure.

**One attempt, never a re-send.** The paste worker does not retry, and the
whole attempt is capped at `INJECT_TIMEOUT_S` (60 s). A `send-keys` that is
merely slow is still in flight and a second one would paste the same image
twice; and `subprocess.run(timeout=...)` kills the client, which leaves it
genuinely unknown whether the first one landed. Bounding one attempt is the
only shape that cannot double-paste. The wording matters for the same reason:
a user told "upload failed" reruns the press, and the eventual paste plus the
rerun's paste put the screenshot in the prompt twice — the failure mode the
honest wording exists to prevent.

**Where the late verdict goes, and why not to the bar.** A flagged
(`?project=`) upload already has a narrator, and the tempting completion flash
would reintroduce the second writer under the exact condition this code path
exists for: when the status line is slow, the listener's own closing message is
still queued in its pump while the worker finishes, so the two would race and
the bar could show "pasted" and then "paste pending". Cross-process ordering is
not available and is not worth inventing here. The deferred verdict therefore
goes to `upload.log` as a WARNING naming the project and the wait it cost
(`inject project=… finished late after 41.2s pasted=True`) — and to the pane,
where the pasted path is its own proof. The listener's closing message is
already terminal and already true: the image is saved, and the paste is
pending.

**The phone is the other client, and it was the last one still lying**
(2026-08-19). `altv` read all three states from the day the reply grew them;
the mobile upload page's JS still read `d.injected` alone, so the same slow
paste that the status line now narrates honestly rendered on a phone as a
failure-looking result — about a file already on disk. It reads all three now,
in the same vocabulary: `injected` → "pasted into <project>", `inject_pending`
→ "saved - psmux is slow, paste still pending" **in the success tint** (a
`pend` marker class dashes the border; it never takes the error red), and
neither → the plain "sent" it has always been. That last one stays a success
deliberately: with no psmux installed, or with inject off, both flags are false
and nothing failed — a page that called that state a failure would be the same
lie pointed at a different user. The only failure on that page is `ok:false`.

The proof is a real browser against a real multiplexer that stalls only
`send-keys`, past `INJECT_GRACE_S` (`tests/e2e/test_upload_browser.py`,
`stalled_serve`). Its assertions read every class the result surfaces ever
took — recorded by an in-page `MutationObserver` installed before the upload —
because the page resets the drop zone two seconds after an outcome, and the
question is not what it shows now but whether it ever showed red.

### Doctor names the wedge, and the probe that finds it cannot join it (2026-08-19)

Twice on the live 40-session host: every psmux control command — `has-session`,
`list-sessions`, `new-session` — hung forever, from any console, including
sockets that had never existed. ConPTY itself was fine (a raw pywinpty spawn
was instant). The whole fleet looked dead for hours.

It was not dead. The holders were `conhost.exe` processes whose parent chain
reached a dead pid or a `psmux.exe`; killing exactly those 14 of the box's 874
conhosts unwedged psmux instantly (`new-session` went from an infinite hang to
892 ms), and **every session then probed alive**. So the reaction the outage
invites — mass-restart, or a reboot — was the one action that would have
destroyed 40 live agents. That is the fact `magent doctor`'s `psmux wedge`
check exists to put in front of whoever finds the machine next: the sessions
are FROZEN, not dead; kill only those conhosts; nothing else is needed.

**Why it is a responsiveness probe and not a liveness sweep.** "Which sessions
are live" has exactly one owner (`psmux.live_sessions`) and this must not
become a fourth answer to it. `psmux.probe_control_plane` enumerates nothing,
names no configured session, and runs `list-sessions` on a throwaway socket no
session name can collide with — a control command on a FRESH socket hanging
*was* the incident's own reproduction, and `list-sessions` starts no server, so
a doctor run leaves nothing behind. A version flag would be cheaper and would
prove nothing: `psmux -V` never touches the plumbing the wedge holds.

**`subprocess.run(capture_output=True, timeout=…)` is not a bound on Windows.**
The probe was built that way first and answered its 5 s timeout in 90 s. On
expiry `run` kills the direct child and then calls `communicate()`, which waits
for the pipe write ends to close — and a grandchild the wedged client left
behind still holds them. The probe now discards output (`DEVNULL`), which has
nothing to wait on; the timeout is a real bound again. This was caught by
`tests/e2e/test_doctor_wedge.py`, whose ceiling is on the whole `magent doctor`
run rather than on the probe, against a real executable named `psmux` that
records its argv and then stops answering.

The enrichment (`procs.count_processes`, a Toolhelp snapshot: how many
`psmux.exe` are resident) is optional by construction — it corroborates the
finding, costs no subprocess, and answers `None` rather than `0` when it cannot
look, because "0 psmux.exe resident" printed on a machine nobody counted would
be an invented fact.

### One log file, many processes (2026-08-19)

A log NAME in `~/.magent/logs/` is not owned by one process, and nothing in the
design ever said it was. Traced writer sets:

| file | processes that write it |
|---|---|
| `hotkey.log` | the Alt+V listener (`python -m magent hotkey`), `magent serve` (`upload_server._supervise_hotkey` / `supervision_enabled`), any foreground `magent up`/`attach` (`launch.ensure_hotkey_listener`) |
| `launch.log` | the foreground CLI (`launch`, `tiling`, `sessions`), `magent serve` (every `psmux.send_keys` / bring-up warning), the listener's F2 path |
| `attention.log` | `magent attention -d`, `magent watch`, `magent status` — anything that READS the agent-state store and hits `agent_state._warn_unusable`, plus `launch.UploadServerSupervisor` |
| `upload.log` | `magent serve`, and `psmux`'s flash-timeout warning wherever it runs |
| `platform.log` | every process that imports a platform backend at all |
| `reap.log` | `magent serve` (the idle reaper's sweep thread: parks, veto changes, warnings), and any process that reads `reap.threshold_s` below the floor (`magent doctor`) |

`logging.handlers.RotatingFileHandler` is a single-process design, and it fails
**silently and expensively** when shared. It keeps the file open for the
process's lifetime and rotates by renaming it out from under itself. On Windows
a second process holding that file open makes the rename fail:

```
PermissionError: [WinError 32] The process cannot access the file because it is
being used by another process: '…\logs\mplogtest.log' -> '…\logs\mplogtest.log.1'
```

`doRollover` has already dropped the stream by then, so the record goes to
`handleError` (a traceback to a stderr no detached daemon has) and is LOST — and
the next record retries the same doomed rename, whose reopen now fails too
(`[Errno 13] Permission denied`; the rename left the path delete-pending). So it
is not one bad record: the log stops rotating *and* stops recording for as long
as contention lasts. Measured on this box with 4 real writers × 200 records:
**272 of 800 records lost**, and the backup chain came out with holes (`.3` and
`.8` missing, clobbered by overlapping rename cascades). On POSIX the rename
succeeds instead and the losing process keeps writing into the file it renamed
away, so those records land in a backup the next rotation overwrites — the same
loss, quieter. The second, independent hazard is that **Windows has no atomic
append**: the CRT implements `open(path, "a")` as seek-to-end followed by write
with nothing holding the file in between, so two overlapping writers resolve the
same offset and one lands on top of the other (the finding `test_altv_flash.py`
records for its own recorder shim).

**Decision: hold no file across records; serialize each record with a
cross-process lock.** `log._SharedRotatingFileHandler` takes an exclusive lock
on a sidecar (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere — the same
per-OS split `lockfile.py` already makes), then opens the log, rotates it if it
has crossed `maxBytes`, writes, and closes, all inside that lock. Because nobody
holds the log outside the critical section the rename can never be blocked, two
writers can never rotate the same file twice, and no two writes can resolve the
same offset. Both OS locks are released by the kernel when the holder dies, so a
crashed writer cannot wedge the others.

**That platform split is bound once at import, never per record** — a trap this
change fell into and CI caught on every POSIX leg. The OS does not change while
a process runs, but `sys.platform` does: tests monkeypatch it to drive the win32
branches of platform-specific code (`upload_server.stop_server`'s taskkill path
is one), and that code *logs*. A logger that re-read `sys.platform` per record
therefore tried to `import msvcrt` on Linux and took down the very call it exists
to observe. `tests/unit/test_log.py::…::test_a_faked_sys_platform_cannot_break_logging`
pins it in both directions.

Rejected alternatives:

- *Per-process files* (`<name>-<pid>.log` plus a stale-pid sweep) — rotation-safe
  and lock-free, but it changes the on-disk layout that a dozen consumers depend
  on (the soak tier's `glob("<name>.log*")`, the dist/e2e/platform tiers that
  read `logs/upload.log` and `logs/attention.log` by name, and every human who
  greps `~/.magent/logs/hotkey.log`), and it scatters one narrative across N
  files. A logging fix must not make the logs harder to read.
- *Tolerating the failed rename* (catch `OSError` in `doRollover`, retry later) —
  minimal, but it only addresses the crash. Records still interleave, the losing
  process still writes into a renamed-away file on POSIX, and the file grows
  unbounded exactly when contention is worst.
- *`concurrent-log-handler`* — off the table; no new third-party dependency.

The price is one lock + one open/close per record: **13 µs → 235 µs** on this
box. These are lifecycle logs at a few records a second, not a request stream,
so the cost is unobservable and the correctness is not.

Three loudness rules ride along, all stricter than the stdlib's. A rotation that
still fails **writes the record anyway** and reports the rotation failure through
`handleError` (the stdlib drops the record instead). A lock that cannot be taken
within `_LOCK_TIMEOUT_S` degrades to an unlocked write — keeping the record,
which is the whole point — and says so once per process **in the log file
itself**, because that is the only channel a detached daemon has and reaching for
`get_logger` from inside a handler would recurse. And a record that cannot be
encoded — a filename carrying a lone surrogate — **lands escaped, never
dropped**: the stream is opened with `errors="backslashreplace"`, where the
stdlib's strict default prints `--- Logging error ---` and loses the record.

The public seam is unchanged (`get_logger(name)`, one handler, still a
`RotatingFileHandler`, still `<name>.log` + `<name>.log.N`), so no consumer
moved. The interlock sidecar is deliberately `<name>.lock` and **not**
`<name>.log.lock`: `glob("<name>.log*")` is how rotated files are enumerated, and
a lock file must not read as a log file. `tests/unit/test_log.py` pins that.

Proof: `tests/e2e/test_log_multiprocess.py` (marker `e2e`, all three OSes) drives
N real writer processes released off a shared wall clock, with a tiny `maxBytes`
so rotation is forced repeatedly *during* the burst, and asserts on what is on
disk — every record present exactly once, no torn line, no `--- Logging error ---`
on any child's stderr, rotation demonstrably happened, and the retained files sat
well under capacity so "missing" can only mean "lost", never "aged out". It goes
RED on the old handler with the traceback above.

### The interactive path outranks the fleet (2026-08-27)

**Symptom.** Typing into a magent pane lagged badly whenever the box was under
load — a visible delay between key and echo — while an ordinary Windows textbox
on the same machine at the same moment stayed snappy. So it was not "the
machine is busy"; it was *this* path being busy-starved.

**Why that path is the one that loses.** A keystroke's echo crosses Windows
Terminal → the psmux attach client → a named pipe → the psmux server → the
ConPTY child, and all the way back, with the client repainting off a ~10 ms
poll. Every hop is a separate process, and three facts about those processes
compound:

1. All of them run at `NORMAL_PRIORITY_CLASS`. Measured live: 169 `psmux.exe`
   on the reporting machine, not one of them above normal.
2. None of them gets the foreground-window boost Windows gives an interactive
   app, because none of them owns a window — psmux is windowless by design.
3. psmux never calls `SetPriorityClass` anywhere, so nothing was ever going to
   change that on its own.

Meanwhile the fleet those panes host — a dozen agents, their language servers,
their builds — is genuinely CPU-hungry and *does* compete. The relay carrying
the human's keystrokes was scheduled as an equal of the work it exists to let
the human steer.

**Decision: raise every psmux process to `ABOVE_NORMAL_PRIORITY_CLASS`, and
keep it there with an idempotent sweep.**

*Why `ABOVE_NORMAL` and not `HIGH`.* These processes are I/O-bound — blocked on
a pipe, not spinning — so what they need is to be *picked* promptly when a key
arrives, not to be given a larger share of CPU. `ABOVE_NORMAL` wins the wake-up
race and costs the compute fleet essentially nothing, because a process that is
blocked consumes no quantum however high its class. `HIGH` is a different
promise (it outranks most of the system, including things a user is entitled to
have go first), and unlike `ABOVE_NORMAL` it is the class where a runaway
starts to hurt. It also matters that raising one's own processes to
`ABOVE_NORMAL` needs **no elevation** — this is a feature that must work on a
normal user's desktop with no UAC prompt, or it is a feature nobody has on.

*Why a sweep and not a spawn-time flag,* which is the part that decides the
whole shape: a Windows priority class is **not inherited by grandchildren**, and
magent never `CreateProcess`-es the psmux SERVER at all — the one-shot psmux
client forks it. There is no magent-owned spawn for a flag to ride on. Anything
that only acted at creation time would therefore boost the client that exits a
second later and miss the server that lives for eight hours. An enumeration of
the live process list is the only thing that can reach the process that matters,
which is why this is a background job rather than a launch argument.

*Why it never downgrades.* The sweep raises from `NORMAL` / `BELOW_NORMAL` /
`IDLE` and touches nothing else. `HIGH` and `REALTIME` are absent from
`procs._RAISABLE_FROM` on purpose: somebody — a user, another tool, the process
itself — put a process there deliberately, and a job that ran every 30 seconds
and quietly demoted it would be a background sweep overruling a foreground
decision. (`GetPriorityClass` answers 0 on failure, which is in no set here, so
a failed read can never be mistaken for a boostable `NORMAL`.) Per-pid failures
— a pid that exited between the snapshot and the `OpenProcess`, another user's
process, a protected one — are skipped rather than raised, because an aborted
sweep boosts an arbitrary *prefix* of the fleet, which is worse than not
sweeping at all.

*Why three owners.* Each covers a hole the other two leave, and the seam
(`psmux.boost_priority`) is identical for all three so "who boosts" is never a
question about behaviour:

- **The launch path** (`launch._start_psmux_and_upload`) boosts the fleet it
  has just created — the moment the boost is most obviously owed.
- **The attention daemon** re-sweeps on every poll, which is what catches
  sessions born later: `magent attach`, `magent up`, a hand-run `psmux
  new-session` hours after the last bring-up.
- **`magent serve`** sweeps too, on its own daemon thread. This is the one that
  matters most in practice and the reason two owners were not enough: on a real
  box serve is effectively always running (every upload and every Alt+V press
  goes through it, and `attention -d` revives it) while the attention daemon
  frequently is not. It is a separate thread from `_supervise_hotkey` rather
  than a branch inside it because that supervisor returns early on
  `MAGENT_HOTKEY_SUPERVISOR=0`, and a user who owns their listener's lifetime
  has said nothing whatsoever about process priority.

Three owners are safe precisely because the sweep is idempotent and cheap — one
Toolhelp snapshot plus one `OpenProcess` per psmux pid, single-digit
milliseconds — and because it logs only transitions, so a steady state is
silent rather than the loudest line in the file.

*The image-name set,* decided against the real artifact rather than assumed: the
Windows release zip (v3.3.6 and v3.3.8 alike) ships the same binary three times
— `psmux.exe`, `pmux.exe`, `tmux.exe` — and `Expand-Archive` drops all three
side by side, so which name a running server carries is whichever one was
invoked. `psmux.PSMUX_IMAGE_NAMES` claims the first two. `tmux.exe` is
deliberately excluded: that name is not psmux's to claim (an MSYS2 / Cygwin /
Git-for-Windows box can carry an unrelated `tmux.exe`), and a sweep that reached
it would be re-prioritising a process magent never launched and knows nothing
about. The cost of the omission is bounded and visible — a user who invokes the
tmux-named copy keeps today's `NORMAL`, i.e. today's behaviour.

*The kill switch is a test-isolation law, not a preference.*
`MAGENT_PSMUX_BOOST=0` joins `MAGENT_HOTKEY_SUPERVISOR` and
`MAGENT_UPLOAD_SUPERVISOR`, and it is the sharpest of the three: this sweep is
the only thing in the product that reaches processes it did not spawn, matched
by IMAGE NAME, and no HOME redirect can contain that. A test that started a real
`serve` or `attention -d` on the developer's box would otherwise re-prioritise
that box's entire live fleet. `tests/conftest.py` pins it off for every tier and
every fixture that builds an explicit child `env=` sets it alongside the other
two.

Layering: the ctypes primitive lives in `procs.py` (the leaf that already owns
`pid_alive` and `spawn_unjobbed`), and `count_processes` was refactored onto the
same `snapshot_processes` walk it needed — a second copy of a Windows process
primitive is exactly how one of them silently rots, the lesson `spawn_unjobbed`
already encodes. The psmux-specific policy (which names, which env gate, the log
line) lives in `psmux.py`, the module that already owns every other fact about
the psmux binary.

Proof: `tests/unit/test_psmux_boost.py` drives the injected
enumerator/setter seams — only-matching-names-opened, never-downgrades,
idempotent, per-pid failure tolerated, kill switch honoured, off-Windows no-op,
and all three owners shown calling the one seam.
`tests/unit/test_procs.py::TestRaisePriorityAboveNormal` is the only test that
changes a real process's priority, and it does so against a child it spawned and
kills itself — never a pid it merely found.

### The modifier is resolved before the multiplexer sees it (2026-08-27)

psmux drops key MODIFIERS in transit. Verified on the live fleet: Ctrl+Backspace
reaches the child as a plain Backspace (no word-delete) and Shift+Enter as a
plain Enter — which in Claude Code SUBMITS instead of inserting a newline. Both
are daily-driver keys; neither failure is the terminal's fault, and no amount of
configuration inside the pane can recover a modifier that never arrived.

The correct fix is upstream's: win32-input-mode (psmux#159), which encodes the
full key event rather than a decoded byte. That PR died unmerged; we filed
psmux#610 / #611 to revive it. Until one of those lands, the only place with
both halves of the chord is the TERMINAL — so that is where magent fixes it. A
Windows Terminal `sendInput` keybinding translates the chord locally and writes
the resulting BYTES into the pty, and a byte has no modifier left to lose:

- `ctrl+backspace` → `0x17`, the Ctrl+W word-erase byte every readline already
  honors — the same workaround VS Code ships. Works through psmux **today**.
- `shift+enter` → `0x1b 0x0d` (ESC CR), byte-for-byte what Claude Code's
  `/terminal-setup` installs. Works outside psmux now and inside it once
  upstream fixes its ESC+CR decode; installing it is correct either way.

Why magent ships this rather than pointing at `/terminal-setup`: that command
**refuses to run inside a tmux/psmux pane**, which is exactly and only where
magent users are sitting when they hit the bug.

Four properties the implementation is built around, each of which bit us live:

1. **Round-trip the whole document, and refuse what you cannot parse.** Windows
   Terminal accepts JSONC; the stdlib parser does not. A file magent cannot
   parse is a file it must not REWRITE — `json.dump`-ing a guess would silently
   delete the user's comments. `SettingsParseError` is a clean refusal that
   prints the exact snippet to paste by hand, and nothing is written.
2. **Control characters are ESCAPE TEXT in the file, never raw bytes.** A raw
   `0x17` is invalid JSON and can break Windows Terminal outright. `BINDINGS`
   holds real control characters in Python and `json.dump`'s default
   `ensure_ascii=True` is what converts them — so nothing hand-writes the
   escapes, and a unit test pins the literal escape text (backslash-u-0017, and backslash-u-001b backslash-r) in the
   written bytes rather than trusting the encoder.
3. **Two schema generations, and the file's own shape wins.** Modern (1.16+)
   splits an entry in two — `actions` carries `command` + `id`, `keybindings`
   carries `id` + `keys`; legacy carries `command` + `keys` inline (under
   `actions`, or under `keybindings` in the oldest files). `detect_schema`
   reads which one the file is written in and `_add_binding` matches it. The
   `id`s are stable strings, because a reinstall that invented a new one would
   grow a duplicate action every run.
4. **Idempotent, and the user's binding always wins.** Already bound to our
   exact `sendInput` → report and write nothing. Bound to ANYTHING else → warn,
   skip that key, and still install the other one; conflicts are matched
   through normalized key spelling (`Backspace+Ctrl` is the same chord as
   `ctrl+backspace`) because a conflict we fail to SEE is a binding we would
   silently duplicate. A timestamped backup lands beside the file before any
   write.

Surfaces: `magent terminal install` / `magent terminal status` (shaped after
`magent hooks`), plus a `wt-keys` check in `magent doctor`. That check is
WARN-at-worst by deliberate choice — a missing binding costs a word-delete, not
a working fleet, and doctor's exit code is what CI and `magent status` read.

Layering: the engine is the leaf `wt_keys.py` (stdlib + `magent.env` only, no
I/O beyond the file it is handed) and the resolver is a seam
(`candidate_paths` / `find_settings`, plus `--settings-file`), so no test and no
smoke run ever touches a real settings.json. The OS gate is a capability probe
(`Platform.supports_wt_keybindings()`), not a `sys.platform` branch.

Proof: `tests/unit/test_wt_keys.py` — both schemas, the escape-text pin, the
JSONC refusal (file byte-identical, snippet printed), the conflict skip with the
other key still installing, the no-op rerun, the backup contents, and the
off-Windows message. `tests/unit/test_doctor.py::TestCheckWtKeys` pins the
check's four states and that it never fails a doctor run.

### Native local paste is opt-in; the pipeline is the default everywhere (2026-08-31)

The Alt+V capture/upload/inject pipeline exists to move an image between
MACHINES: a laptop viewer's clipboard to the desktop host over `magent attach`,
or a phone screenshot to the host over the upload page. Run on the same machine
it looks like pure overhead -- an agent that honors a paste keystroke reads
the clipboard ITSELF, and locally that clipboard is the very one the user
copied into. So with `MAGENT_ALTV_NATIVE=1`, a press whose listener manifest
carries no ssh host short-circuits to `altv.native_paste`: one `send-keys C-v`
(0x16, which psmux delivers to the pane as a functional Ctrl+V), and the agent
pastes a real attachment. Nothing is captured, nothing is uploaded, nothing
lands in `~/.magent/uploads`.

Three boundaries were chosen deliberately:

- **The fork reads the manifest's `ssh_host`, not the serve URL.** A loopback
  `server_url` cannot mean "local" -- every test fleet is loopback, and so is
  a tunnel. The ssh host is already the listener's self-description (F2 routes
  on it), and it is null exactly when the panes this listener serves run on
  the pressing machine.
- **The send mirrors the server's inject exactly** -- same primitive, same
  `-t` target, same bounded one-attempt law. A killed `send-keys` may or may
  not have landed, so there is no retry and no fallback to the upload path: a
  fallback after a landed C-v is how a screenshot gets pasted twice, the same
  double-paste the inject path already refuses to risk.
- **The fork is OPT-IN (`MAGENT_ALTV_NATIVE=1`), not opt-out.** It shipped
  default-on in 3.15.0 and was reverted the same day: Claude Code on Windows
  acts only on a PHYSICAL Ctrl+V and ignores the injected 0x16 -- verified
  live by injecting the same byte into a PSReadLine pane (pastes the
  clipboard) and a Claude pane (nothing) -- so the default-on fork reported
  `ok-native` while the user saw a dead hotkey. Delivery is not the failure
  (psmux hands the pane a real Ctrl+V); the agent's input stack is, kin to
  the psmux modifier-drop lore one layer deeper. The upload path's path-text
  inject is the one delivery every agent demonstrably accepts, so it stays
  the default, and the `boost_enabled`-style degradation (a listener must
  never die of a bad environment) degrades to the upload path too. The trap
  that let this ship: the native e2e proves the recorded argv through a shim,
  and the real-psmux interaction tier pins the upload chain under the
  opt-out -- no automated tier drives an injected C-v into a REAL agent pane,
  so "the agent heard it" was assumed, not proven.

The narration keeps its acknowledgement-first law with a shorter script:
`pasting...` then `pasted from clipboard` / `paste key not delivered -
clipboard still has the image` -- the failure wording carries the one honest
comfort the native path always has (nothing was consumed).

Proof: `tests/unit/test_altv.py::TestNativePress` (one C-v, no capture, no
upload, ack-before-send, vocabulary membership) and
`tests/e2e/test_altv_flash.py::TestNativeLocalPress` -- the REAL spawn path,
asserting the recorded argv, the untouched uploads dir, and the narration
order. The e2e fixture is the sharp edge to know about: `native_paste` spawns
psmux from the LISTENER process (the test itself), so the recording shim must
win the test process's PATH and `find_psmux`'s lru cache must be cleared both
ways.

### A local file is pasted, not uploaded (2026-09-25)

Every upload path accepts any file type, and Alt+V also takes files copied in
Explorer (CF_HDROP, which wins over CF_DIB when both are on the clipboard). The
decision that shapes it is what a LOCAL press does with them: it pastes the
files' ORIGINAL absolute paths (`altv.paste_paths`) and nothing else. No copy
into `~/.magent/uploads`, no upload, no size cap. The upload exists to move
bytes between machines; on the machine that owns the pane the agent can
already open the file where it lives, and a copy would only add a stale
duplicate, a disk cost, and an arbitrary 100 MB ceiling on a file that never
travels. "Local" is the same fork `native_paste` reads -- the manifest's
`ssh_host` is null -- and unlike native paste this fork is NOT opt-in, because
what it delivers is path TEXT, the one thing every agent demonstrably accepts
(see above).

The remote half and the edges:

- **One request, one paste.** A remote press sends every file as a `file` part
  of ONE `/upload` request, and the server saves them all and makes ONE paste
  of `sessions.paths_line(saved)`. N requests would be N pastes racing into one
  input line, and a paste is one attempt (the double-paste law); a separate
  "paste this text" endpoint was the other way out and was refused, because the
  upload server has no auth and must not grow a keystroke-injection verb.
  The phone page uses the same shape: its picker takes several files and its
  Ctrl+V stages every copied file, and one Send is one request. Consequence:
  the 100 MB cap is per request, i.e. per press or per send.
- **The line is quoted only where it has to be.** A path of plain characters
  goes bare, so a single ordinary path is byte-for-byte what the one-image
  inject always pasted. Otherwise double quotes, which is what Windows Terminal
  writes when a file is dropped on it; a path containing `"`, `$` or a backtick
  (which a double-quoted POSIX string still interprets) is single-quoted with
  `'\''` escaping. Both sends -- the local paste and the server's inject -- are
  `-l` (literal): the line is text and must never be read back as a psmux key
  name. What quoting cannot make safe is refused outright: a path carrying a
  control or line-break character (Unicode categories Cc, Zl, Zp -- newline,
  ESC, NEL, U+2028/U+2029) could end or submit the input line it lands in, so
  `sessions.paths_line` raises rather than build that line, and a local press
  of such a path is refused whole (`path-unpasteable`, nothing typed). The
  server never meets one: the names it saves are sanitized to word
  characters, dots and dashes.
- **A folder refuses the whole press.** Uploading a directory has no single
  honest meaning (recurse? archive? the listing?), and a mix that uploaded only
  the files would hand the agent a selection the user did not make. Nothing is
  read or sent; the bar says `folders not supported - copy files`. The page
  refuses in the same words, but its detection is weaker than Alt+V's `stat`:
  a browser hands a folder over as a `File`. Only a paste or a drop can carry
  a folder -- the picker cannot select one, so a plain pick is never checked.
  On a paste or a drop, the item's entry (`webkitGetAsEntry()`, Chromium)
  decides whenever the browser exposes it, in both directions. Only an item
  with NO entry falls back to the shape a folder arrives as: an empty file the
  browser has no MIME type for. That guess also matches a genuinely empty
  `.toml`, `.log`, `.gitkeep` or `Makefile`, which is why it is confined to
  items the browser gave no entry for (it once overruled the entry on the
  picker, refusing an empty `.toml` Chrome had reported as a file). The
  browser tier can only drive the fallback (a synthetic paste cannot carry a
  directory entry), so the entry path is proven by nothing but the real
  browser.
- **The limit is one number with three enforcers, and it counts FILES.**
  `sessions.MAX_UPLOAD_BYTES` (100 MB, in `sessions` so `altv` stays a leaf
  that never imports the server) is the sum of the file sizes: the page checks
  that sum before it sends, Alt+V checks it from a `stat` BEFORE any file is
  read (an oversized press costs neither memory nor a round trip), and the
  server checks the same sum once parsed (413 naming the limit). The request
  itself may carry `upload_server.MULTIPART_ALLOWANCE_BYTES` (1 MiB, fixed and
  named) of multipart framing on top; a larger Content-Length is refused
  before a byte is read. Comparing Content-Length to the files limit, as the
  server once did, refused selections a few hundred bytes under it that both
  pre-checks had passed.
- **`_DRAIN_CAP_BYTES` sits past the request ceiling.** The drain exists so an
  honest client that sent just over the limit reads the 413 instead of a
  Windows RST (the kernel resets a socket closed with unread bytes). A drain
  that stops at the ceiling leaves exactly that client's tail unread, so it
  reaches 1 MiB past it; a bigger overshoot is cut off and the connection
  closed. It reads in 64 KB chunks, so the cost is time on a refused request,
  never memory.
- **A body is whole or it is nothing.** The server reads exactly the declared
  Content-Length and parses with `memoryview` slices of that one buffer (at
  100 MB a request, split-and-slice copies held four bodies at once). Fewer
  bytes than declared, or a last part whose closing delimiter never came, is
  `400 Upload incomplete` with nothing saved and nothing pasted -- a cut-short
  part still has its headers, and saving it would announce a truncated file as
  uploaded. Each connection has a per-OPERATION socket timeout
  (`CONNECTION_TIMEOUT_S`, 60 s), so a client that declares a body and stalls
  is let go the same way instead of pinning a handler thread, while a slow
  upload that keeps moving is never cut off.
- **Alt+V streams, under a boundary of its own.** A remote press sends its
  files straight off disk, a block at a time, with an explicit Content-Length
  (the server speaks no chunked encoding). As one `bytes` body it went out in
  a single `sendall`, whose socket timeout is a TOTAL budget -- a large
  selection over a slow link failed as "cannot reach magent serve" while still
  moving -- and the long-lived listener held every file plus a joined copy.
  Each request draws a random boundary (`secrets.token_hex`), as browsers do:
  with a fixed one, any file containing that line was cut there and the server
  said ok. A copied file that is gone, held by another app, or shrinks under
  the send is named (`file-missing` / `file-unreadable`), never "unexpected
  error" and never "cannot reach magent serve".

A file keeps its own name (only a nameless clipboard blob gets a generated
`paste-<ts>` one), because the name is often the most useful thing the agent
is told about it -- dotfiles included: `.env` lands as `<stamp>_.env`, the
prefix already keeping it from being hidden. The kept part is capped at 150
UTF-8 bytes by trimming the stem on a character boundary, extension intact,
so a legal 250-character name cannot push `<stamp>_<name>` past one path
component's 255 limit. Same-named files never overwrite each other, within a
request or across two in the same second: each name is RESERVED with an
exclusive create and bumps to `<stamp>_<n>_<name>` until one create wins (an
`exists()` check cannot see a name another request chose but has not written
yet), and a request that is refused or fails part-way removes what it
reserved.

Proof: `tests/unit/test_altv.py::TestFilePress` / `TestPathsLine` /
`TestTheRemoteBodyIsStreamed` / `TestARemoteFileThatWillNotRead`,
`tests/unit/test_upload_server.py` (any-type byte-identity, several files, the
limit at and one byte past the files cap, cut-short and stalled bodies,
`TestParseMultipart`, `TestDestFor`), `tests/unit/test_hotkey.py::TestClipboardFiles` (a synthetic
DROPFILES block through the real `DragQueryFileW`), and
`tests/e2e/test_altv_flash.py::TestFilePress` -- the real spawn path for the
remote upload, the local original-path paste and the folder refusal.

### The host brings itself up on its own desktop (2026-09-12)

`magent attach <host>` asks the host to run `magent up` over ssh, and on
Windows OpenSSH is a **service** -- so every process that bring-up creates is
born in logon **Session 0**, the services session, which has no desktop
composited onto any monitor. Measured, once: 82 psmux servers and 42 Claude
agents alive there, plus a `magent serve` holding `127.0.0.1:8034`. The desktop
could not see any of it. `magent status` called those sessions stopped; every
desktop bring-up logged "session never came up after respawn" for exactly those
names, because psmux's registry under `~/.psmux` is shared across sessions and
a held name makes `new-session` exit 1; tiling logged "window not found"; and
the desktop's Alt+V talked to a server that could see none of the desktop's
sessions. Clearing it took an elevated kill of 1172 processes -- Windows
OpenSSH hands an admin the full token, so those servers also outranked the
desktop user's own shell.

The fix is **not** to detect it and warn. A bring-up run in the wrong session
is not a degraded bring-up, it is a fleet that has to be destroyed by hand, so
the command re-runs itself where it belongs.

**Task Scheduler, not `CreateProcessAsUser`.** The API route needs a token
from another session, which means `SeTcbPrivilege` -- an ELEVATED magent -- for
something the user is plainly entitled to do to their own desktop. A one-shot
`/IT` task ("run only when the user is logged on") needs no stored credential,
no admin right and no password -- hence no `/RU`/`/RP` -- and it is *Windows*
that places the process in the interactive session rather than magent picking
one. That last point is the important half, and it is why
`supports_desktop_handoff()` reads `WTSGetActiveConsoleSessionId` only to ask
"is there a usable console session AT ALL" and never to choose one: that id can
name an RDP session that is not the physical desktop, so every "find the
interactive session" heuristic is wrong on some real machine. When the answer
is 0 or `0xFFFFFFFF` the disposition falls to `refuse` with its own wording --
nobody is logged on, so "run it on the desktop" would be advice the user cannot
take. Verified on the incident machine from a real Session-0 sshd login:
Session 1, Medium integrity, desktop visible, ~1.6s. `/ST 00:00` is deliberately
in the PAST, so a task stranded by a killed caller (an ssh drop takes the
host-side `magent up` with it) can never fire on its own; `/Run` ignores the
trigger entirely.

**The task runs a three-line PowerShell file, and the command is not in it.**
`/TR` truncates SILENTLY past ~261 characters, so it carries only a fixed
launcher (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File <run.ps1>`;
Windows PowerShell, not `pwsh`, which is not on every box). `run.ps1` sets
`$ErrorActionPreference = 'Stop'`, exports `MAGENT_SESSION0_POLICY=refuse`, and
runs `& '<python>' -I '<scratch>\launch.py'` -- the interpreter magent itself is
running under, and `launch.py` a byte-for-byte copy of
`platform/_handoff_launcher.py`. The argv travels in `argv.json` (ASCII json:
every non-ASCII code point escaped, lone surrogates included), never as a
command line, so there is no quoting layer between the user's arguments and the
child: the launcher hands the list to `subprocess.Popen`, whose `list2cmdline`
is the one quoting pass, by the MS C-runtime rules the child's own parser uses.
The only things PowerShell sees are two paths, each ONE `_ps_quote`d literal.
The file is written UTF-8 WITH a BOM, because 5.1's `-File` reads a BOM-less
script as the ANSI code page and a scratch or interpreter path holding `Ñ` or
`т` would reach PowerShell corrupted; a path with no UTF-8 form at all (a lone
surrogate, which Windows allows in a directory name) is refused before any
task exists.

**The launcher is Python because PowerShell cannot hold the handle.** The
previous launcher was `Start-Process -PassThru -RedirectStandard*`, then
`$null = $p.Handle` and `$p.WaitForExit()`, and it lost exit codes. Windows
PowerShell 5.1's redirecting `Start-Process` closes the handle CreateProcess
returned, and `.Handle` re-opens the process BY PID, after the fact: a child
that had already exited left `.ExitCode` at `$null` and `rc.txt` empty, and the
hand-off reported an "unreadable exit code" for a bring-up that had worked.
Touching `.Handle` early only narrowed that window; nothing in PowerShell can
close it. `subprocess.Popen` keeps the CreateProcess handle, so `wait()` reads
the code however fast the child was. The race is pinned deterministically, not
by timing: `tests/unit/_jobhook.py` puts the task in a named job object whose
watcher kills the command with code 7 the moment its first thread runs --
before any launcher can look -- and the test asserts that kill happened before
it asserts `rc == 7`. The launcher is stdlib only and imports nothing from
magent (it runs outside the package), and it reads and sets no environment
variable; `-I` keeps PYTHONPATH, PYTHONHOME and the scratch directory off
`sys.path`, so nothing in the user's environment can put a different module
under its imports. The command gets `CREATE_NEW_CONSOLE` with the window hidden
(what `-WindowStyle Hidden` gave it) and stdin on the null device: nobody is at
the desktop to type, and an inherited stdin is the task's console.

The reading side has its own rule: `rc.txt` EXISTING is not the exit code being
READABLE. The PowerShell launcher's `Set-Content` created the file, then wrote,
and refused readers until it closed -- measured, 298 of 300 first reads after
the file appeared were a sharing violation. The poll treated that read as final
and reported the same "unreadable exit code ''" for succeeded commands, a
windows-latest unit flake on five unrelated PRs. The Python launcher renames a
finished file into place, which closes its own window, but a scanner can still
hold a file it has just seen written. So rc.txt goes through the same reader as
pid.txt (`_read_recorded_int`), and only a COMPLETE integer ends the wait --
complete meaning ended by a newline, so the `1` of `12` can never be final
whoever wrote the file. While rc.txt is present the lost-child check
stands down: a launcher still writing it has not lost anything. A present
rc.txt that stays anything else past `_HANDOFF_RC_GRACE_S` (10s) or the budget
gets one last, decisive read, and failing that is its own answer -- the
command finished and we cannot say how -- distinct from "never started",
"lost its child" and "may still be running".

**`schtasks` comes from the system directory, not PATH.** `run_on_desktop` is
reached from an ssh login, and letting that login's PATH choose what runs as
the logged-on user would turn a hand-off into an execution primitive for
whoever set it. The consequence is a deliberate test gap: a real child process
cannot be pointed at a fake, so there is no e2e proof of a SUCCESSFUL hand-off
-- the alternatives were a test-only environment variable or writing real
scheduled tasks on the machine running the suite. The full choreography is
proven in the unit tier through the `_schtasks_exe` seam, with real processes
on both ends of the launcher; the e2e tier proves the detection and that
nothing is ever created in place.

**Two signals, not one.** `WindowsPlatform.logon_session_is_interactive()` is
false when `procs.current_session_id()` is 0 OR when `env.is_ssh_login()` is
true. The second is a fact about Windows, not a test hook -- OpenSSH is a
service, so there is no configuration in which an ssh login lands on the
desktop -- and it covers the case where the ctypes probe answers None. An
UNKNOWN session id counts as interactive: a probe that fails on some future
Windows must never be able to stop an ordinary desktop launch.

**One policy function, two enforcement altitudes.** `launch.session0_disposition`
is the only place the question is answered (`run` / `handoff` / `refuse`,
per `MAGENT_SESSION0_POLICY`). The COMMAND shells hand off: `up` and
`serve --ensure`, the two things `magent attach` fires on a host. The CHOKE
POINT -- `psmux.launch_verified`, which every session magent creates passes
through -- only ever REFUSES, and that asymmetry is deliberate. Reaching the
choke point in Session 0 means a path that did not hand off (`--go`, the menu's
`u`, `revive`), and the honest outcome there is a loud failure naming the cause
rather than a subsystem quietly writing scheduled tasks. The three "N session(s)
failed to come up" printers add `launch.session0_note()` so the casualty list
never arrives without its reason -- last time a user read 40 failed names and
had to find `launch.log` to learn nothing had been attempted.

`allow` exists because a headless Windows host reached only over ssh has no
desktop to hand off to, and Session 0 is genuinely where its fleet belongs.
An interactive session short-circuits to `run` BEFORE the policy is read, so
that setting cannot change a normal desktop launch. Off Windows every platform
reports interactive and nothing changes at all -- tmux over ssh is how people
work there.

Three smaller details that are load-bearing. `argv.json` carries the caller's
working directory and the launcher starts the command there (a scheduled task
starts in `system32`, and `find_config` walks up from the cwd, so the desktop
copy would otherwise bring up a different config's projects). `run.ps1` exports
`MAGENT_SESSION0_POLICY=refuse` for the child, so a hand-off that somehow
landed in Session 0 again cannot recurse -- a recursion whose every level
writes a scheduled task. And the launcher writes `pid.txt` the moment the child
exists and `rc.txt` only after `wait()` returns, each one decimal integer and a
newline written to a temporary name and renamed into place, so the 250ms poll
never reads a half-written one (the exit code is the signed Int32 Windows
tools print, so an NTSTATUS arrives as `-1073741510`, not `3221225786`). That
order is what lets the poll tell four failures apart: no pid after the start
grace, from a task that is not running, means Task Scheduler never ran it (a
launcher that merely could not record its pid -- a scanner holding the file,
a full disk -- carries on, and its rc.txt still answers); a pid that is gone
with no rc means the launcher lost its child and nothing is coming; an rc.txt
that is there but never reads as a complete integer means the command finished
and its exit code is lost (see the reading side above -- rc.txt existing is
not the code being written); and none of those is the caller's budget simply
running out. On that last one the delegated child is deliberately NOT killed:
a bring-up still running on the desktop is doing the work that was asked for,
and the pid is a number Windows recycles freely. A command that cannot be
started at all -- a missing executable, a working directory that is gone --
gets its reason appended to `err.txt` and `rc.txt` = 1 with no `pid.txt`, so
the caller hears it at once instead of after the start grace.

Diagnostics are the other half: `doctor`'s `psmux-session0` check and one
`status` stderr line count psmux servers still stranded there (by image name
plus `procs.session_id_of`, which needs no process handle and so can see the
high-integrity ones). WARN, never FAIL, and additive in `status --json`
(`psmux_session0`): magent did not start them and cannot stop them, so they
must not move the 0/1/3 exit contract.

Proof: `tests/unit/test_desktop_handoff.py` (the policy, the refusal wordings,
the relay, the staged files read back exactly and `run.ps1`'s parsed shape,
the job-object exit-code pin, and the real create/run/poll/delete
choreography against a FAKE `schtasks` installed through the `_schtasks_exe`
seam), `tests/unit/test_handoff_launcher.py` (the launcher itself, on every
OS, against real child processes), `tests/unit/test_attach.py::
TestUpHandsOffFromSessionZero`, `tests/unit/test_serve_port.py::
TestEnsureHandsOffFromSessionZero`, and `tests/e2e/test_session0_handoff.py`
(a REAL `magent up` child told it is an ssh login). `tests/conftest.py` pins
`MAGENT_SESSION0_POLICY=allow` for every tier, and every fixture that builds an
explicit child `env=` carries it: a CI runner is legitimately non-interactive,
and the default would have it writing real scheduled tasks.

### No daemon is planted in Session 0, and the ones already there are named (2026-09-30)

The hand-off covered the two commands `magent attach` fires. Five other paths
still left a SURVIVOR in Session 0 whenever they ran there, and all but the
last are one ssh login away: `--go` and the menu's `u` (a detached serve plus
an Alt+V listener), a foreground `magent serve` (its listener supervisor), and
`magent attention -d` together with that daemon's own upload watchdog. The
cost is sharper than a stranded psmux server's. A Session-0 serve holds the
loopback port, so the desktop's serve dies of "port in use" while the desktop's
Alt+V talks to a server that can see none of its sessions. A Session-0
listener's keyboard hook never sees a key typed at the desktop.

The same two altitudes apply, through the same policy function. The SEAMS every
spawner passes through only refuse, via `launch.session0_block(base)`, which is
`session0_disposition` plus the shared wording: `ensure_upload_server`,
`UploadServerSupervisor.tick`, `start_hotkey_listener`,
`ensure_hotkey_listener` (which can also END a wedged listener), serve's
`_supervise_hotkey` and serve's `AttentionDaemonSupervisor.tick`. The
watchdog's refusal is logged once per cooldown, not
once per poll. The listener seam refuses before `magent.hotkey` is imported, so
the refusal is testable on every OS. Serve's supervisor says it once and stands
down instead of retrying every interval for the life of the server. `--go`
prints the serve refusal in place of an upload URL nothing will answer. The one
COMMAND shell among them, `attention -d`, hands off exactly like
`serve --ensure`: it rebuilds its argv from its parameters, with the same budget
constant and the same refusal wording. `--stop` is never handed off, because
stopping is not planting.

A foreground `magent serve`, `magent hotkey` or `magent attention` typed over
ssh is left alone. It dies with the ssh job and plants nothing that outlives
the typing.

**The ones already there.** `status`, as one stderr line, and `doctor`'s
`daemons-session0` check (WARN at worst) name them, and `status --json` carries
an additive `daemons_session0` count. This follows the `psmux_session0`
precedent: none of it moves the 0/1/3 contract. The lookup works like this:

- **Identity** comes from magent's own pid files. A high-integrity process's
  command line cannot be read from the desktop, so the pid file is the only
  record of which process is which daemon.
- **Liveness and session** come from `procs.session0_residents`: one snapshot
  plus the handle-free `session_id_of`.
- **When to ask** is decided by `active_console_session_id`, and only whether a
  desktop exists. On a headless host Session 0 is where daemons belong.
- **Recycled pids** are filtered out by image name: only `python*`/`magent*`
  images count. A stale pid file must never accuse a service.

That identity source forced one change underneath. `pid_alive` needs a handle,
so it answered False for exactly these processes, and `listener_pid` /
`daemon_pid` deleted their pid files as stale, destroying the evidence on the
first `status`. They now delete only when `procs.pid_gone` holds, meaning no
session either. For a live-but-unopenable pid they return None ("not ours to
use") and keep the file. A pid file written before the last boot is the one
exception, and it is checked first: it is cleared whatever its pid is doing,
because it predates every process now alive, so an unopenable process holding
that number is someone else's. The diagnostic reads the same way (its
`_read_pid` ignores a pre-boot file, read-only), so a service handed a
recycled number is never reported as a stranded magent daemon.

Proof:
- `tests/unit/test_session0_daemons.py` (every seam refuses and the command
  shell hands off);
- `tests/unit/test_session0_diagnostic.py` (residents, `pid_gone`, the kept pid
  files, the collection and the wording);
- `TestSessionZeroDaemons` in `test_status.py` and `TestCheckDaemonsSession0`
  in `test_doctor.py`.

### The attach client is the renderer, so the human's colour setting wins (2026-09-13)

`env.spawn_child_env()` has stripped the launching shell's colour overrides
since the 2026-08-18 incident, on the reasoning that a pane's own shell sources
the profile that should decide its rendering. A user-facing **attach** window is
the other case, and this is the day it cost something: `magent --go` run from a
Claude Code tool shell (which puts `NO_COLOR=1` and `CLAUDECODE=1` into every
subprocess it spawns) opened 57 attach windows that rendered monochrome around
agents that were themselves perfectly colourful — the created sessions went
through the seam, the attach client did not. The psmux client honours
`NO_COLOR`, and for an attach pane that client IS the renderer, so the inherited
environment is the only one it will ever have.

That asymmetry is why the fix is a second, narrower seam
(`env.attach_client_env()`) and not a reuse of the first. For a created session
the launching shell is always the wrong authority. For an attach client it is
usually the RIGHT one: magent is for other people's machines too, and a user who
exports `NO_COLOR` in their own shell means it. So the strip is conditional, and
the condition is an agent-harness session marker (`_AGENT_SESSION_VARS`) — the
harness set `NO_COLOR` for its own tool output, never for the human's windows,
so the marker is proof the override was inherited rather than chosen. No marker,
no dict: the function returns `None` and the spawn inherits, byte-for-byte the
historical behaviour. The psmux/tmux nesting markers survive here either way,
which is the exception `attach_psmux` has always documented — attaching from
inside a pane really is nesting, and psmux's own guard is the right authority on
it. The harness markers themselves survive too: they are identity for a CREATED
agent, and an attach client creates none.

No knob was added, deliberately. A `MAGENT_*` opt-out would ask the user to
configure their way out of a bug they did not cause, and it would need an
`.env.example` entry and a schema pin to carry a question the marker already
answers. Three call sites route through the seam — `attach_psmux` plus the
supervised-pane and `--no-mux` `wt` spawns in `cli/attach.py`. Pins:
`tests/unit/test_env_schema.py::TestAttachClientEnv`,
`tests/unit/test_platform_contract.py::
TestAttachClientKeepsNestingMarkersButNotALeakedNoColor`, and
`tests/unit/test_attach.py::TestAttachPanesLoseOnlyALeakedColourOverride`.

### An idle pane is proven, not read off the foreground (2026-09-26)

`magent up --revive` (and the interactive `up`, which revives without the flag)
types `cmd /c claude --continue` + Enter into every live session whose agent
has fallen back to a bare shell. The bring-up's send-keys verification re-sends
the start command on the same signal, and `status` prints an `idle` column from
it. That signal was `#{pane_current_command}` read as a shell — and psmux
reports the pane's foreground DESCENDANT, not the pane's own process. While
Claude Code runs a tool the reading is `bash` (its Bash tool), `pwsh`, `grep`
or an MCP server, with claude.exe alive under the pane. Measured live on a
31-session fleet: 4 sessions read idle while their agent was mid-turn, and
revive would have typed a second agent's command line into each one's prompt.

The rule now: a pane is idle only on POSITIVE proof, and the one place that
decides it is `psmux.idle_sessions`. All three consumers read it —
`revive_sessions`, `WindowsPlatform._verify_sends_landed` and
`cli/status.py::_psmux_sessions` — and nothing else classifies a pane. A yes
needs all three of:

1. the foreground reading is a bare shell (`is_idle_command`) — kept, because
   a pane in the user's own program is not at its prompt either, and as a
   cheap filter: a session that fails it costs no further probe;
2. the pane's OWN process (`#{pane_pid}`, read by `psmux.pane_pids`) was read,
   is present in the process snapshot, and is itself a shell;
3. nothing in that process's subtree (`procs.process_tree` over the Toolhelp
   snapshot, which now carries parent pids) is an agent image or a live
   launcher (`psmux._LAUNCHER_IMAGES`, i.e. `cmd`).

Everything unknown is a no: an unreadable or non-numeric pane pid, a failed
snapshot, a pane process that is gone by snapshot time, a probe still
unanswered when the fan-out's deadline passes. Off Windows there is no
snapshot, so nothing is ever idle there — revive does nothing rather than
guess. The asymmetry is the point: a false "busy" leaves a dead pane for the
human to restart, a false "idle" types into a live agent's input.

The launcher rule exists because the image list alone is not the fleet.
`config.DEFAULT_TOOLS` ships tools with no registry entry and so no image
(`agy`, `cursor-agent`), and a pane running one of them mid-tool read exactly
like an idle pane. What every such pane does have is magent's own wrapper:
every command magent types is `cmd /c <command>` (`platform/windows.py::
_send_argv`, and revive's own send), and `cmd /c` exits exactly when its
command does. A live `cmd` under the pane's shell therefore IS the launched
command, whatever that command's image is called. The agent images still
matter for the one path with no `cmd` above it: a human who typed `claude` at
the prompt. The cost errs the safe way — a `cmd` the user started by hand reads
busy — and a pane whose own shell is `cmd` was never idle to begin with (`cmd`
is not in `_IDLE_SHELLS`).

The pane probes are bounded as a batch, not one by one. `_display_fan_out`
spawns every `display-message` before reading any and then waits on ONE
deadline (`_FAN_OUT_TIMEOUT_S`); a probe still running when it passes is
killed unread and its session reads unknown, while one that already exited
gets `_FAN_OUT_DRAIN_S` to hand over its output. The per-probe timeout it
replaced made a wedged server cost N x timeout across a fleet of N. Because
the window is paid once, it is sized for a loaded host, not an idle one: 10 s.
Under a spawn storm a single `display-message` runs past 3 s (the measurement
behind `FLASH_TIMEOUT_S`), and a storm is exactly when the send-verify reads
the fan-out. At 5 s one slow start read every pane as unknown and skipped the
re-send. On attach the window bounds a share, not the whole read:
`idle_sessions` runs two fan-outs back to back, so 2 x 10 s bounds its SHARE
of the 30 s ssh read of `up --json --revive`, not that read. The rest of the
path has no finite bound to sum — `live_sessions`' sweep before it is
unbounded on purpose (a slow server must not read dead), revive's
`has_session` pool runs ceil(n/16) waves in series, and each `send_keys` after
it may take `SEND_KEYS_TIMEOUT_S` (20 s) per pane.

The snapshot the verdict rests on is complete or it is nothing: a Toolhelp
walk ends only on `ERROR_NO_MORE_FILES`, and a `Process32NextW` that fails
for any other reason makes `procs.snapshot_processes` return None (unknown,
so not idle) rather than the shorter list it reached — a partial list would
read "nothing runs here" for every process the walk never got to.

The agent images come from the registry, not a second list: each
`AgentTool` carries `images` (`claude`, `codex`), and
`sessions.agent_image_names()` adds `AGENT_RUNTIME_IMAGES` (`node`, the npm
shim either agent can run under). A snapshot carries image names, never
command lines, so ANY node under a pane counts — the reading that errs toward
busy. The cost is batched: one `pane_pids` fan-out (every probe spawned before
any is read, like `pane_current_commands`) and ONE snapshot per call, paid only
when some reading is a shell; status passes the readings it already holds for
its table instead of probing twice.

Two known edges, both measured against the code rather than the fleet. Windows
never rewrites a stale parent pid, so a recycled pid can pull a stranger into a
pane's subtree; that only ever errs toward busy. The other is the one real
hole: a parent-pid walk cannot reach an ORPHAN. If an agent's `cmd` is killed
out of band while the agent lives, the agent keeps the dead `cmd`'s pid as its
parent, nothing in the snapshot leads from the pane's shell to it, and the
pane can read idle with the agent still attached to its console. A caller that
only KILLS what the walk finds is safe there, since it finds nothing to kill.
A caller that TYPES into the pane is not.

The sound closure is known and deliberately not built in this change: ask the
pane's CONSOLE who is on it, not the parent pids. A helper started with
`DETACHED_PROCESS` (it has no console of its own to give up, and the caller's
console is left alone) calls `AttachConsole(pane_pid)` and then
`GetConsoleProcessList`. The pane is agent-free only if every process on that
console is in the pane shell's subtree and none is an agent image or a
launcher; any failure of the helper reads busy. That check is a prerequisite
for the idle reaper, which types a mode reset and a resume into panes it has
emptied, and it should land with it.

*It landed with the reaper (2026-09-27).* The check is now the last stage of
`idle_sessions`: `procs.console_clients` asks one detached helper per call,
and a pane stays idle only when every process on its console is inside the
pane shell's subtree and none is an agent or a launcher. A helper that
fails, times out or cannot attach reads busy. See "A finished, long-idle
agent is parked, not killed".

Pins: `tests/unit/test_psmux.py::TestReviveNeverTypesIntoALiveAgent`,
`::TestIdleSessions`, `::TestTheFanOutWaitsOnOneDeadline`,
`tests/unit/test_platform_contract.py::TestWindowsSendKeysVerification`,
`tests/unit/test_status.py::TestIdleColumnNeedsPositiveProof`,
`tests/unit/test_procs.py::TestProcessTree`, and against a real psmux pane on a
private socket (CI's Windows platform leg only),
`tests/platform/test_real_psmux.py::
test_real_pane_reads_idle_only_while_nothing_it_launched_runs`.
### A slow child is not a failed child (2026-09-25)

Both detaching launchers, `attention -d` and the Alt+V listener start
(`launch.start_hotkey_listener`), learn their child's pid only from the pid
file the child writes once it is up. Both used to wait a fixed 2 seconds for
it. On a loaded desktop the daemon took 4.7-11.45s to register (about 1.5s
idle; the pre-routing build flaked the same way), so `attention -d` printed
"failed to start" and exited 1 over a daemon that then came up and kept
running. The serve-watchdog e2e tier went red on it, and its teardown, which
killed only pids it had learned, left that daemon and the server it supervised
running.

**One wait, one owner.** `procs.await_registration(child, read_pid, ...)` is the
launcher-side wait for both callers. It lives in `procs.py` because that stdlib
leaf is already imported by `launch.py` and by `cli/`: both directions are
legal, and a src module never imports the cli package (LS-A-001). The window is
`REGISTRATION_TIMEOUT_S` (20s, about 1.75x the slowest measured start). The
idle path pays nothing, because the loop returns on the first poll that sees a
pid. The listener start keeps `not_pid=existing`, so a restart whose kill did
not take cannot read the old pid back as the new listener.

**An exit ends the wait at once.** A child that exits, for any code including
the 0 that `magent hotkey` returns when another listener already runs, is
reported immediately rather than waited out. That check is meaningful on
Windows because `spawn_detached` Popens `sys.executable`: under a venv that is
the launcher `python.exe`, which waits for the base interpreter and passes its
exit code through. The direct child's `poll()` therefore tracks the real
process.

**The wait never kills the child.** A timeout means "not registered yet", not
"dead". On the machine this was measured on, the child was usually a few
seconds from coming up. The launcher still reports failure (rc 1) so a script
can react, but it leaves the child alone. Killing it would turn a slow start
into a real failure, which is the bug this fixes.

**The window is a bound.** It is not a knob: there is no env var, because the
right answer is "long enough for a loaded box, and finite". A child that hangs
alive without registering must not stall serve's supervisor thread or a `--go`
launch before tiling. Pins:
- `tests/unit/test_procs.py::TestAwaitRegistration`
- `::TestTheWaitNeverEndsTheChild`
- `::TestTheDefaultWindowIsBounded`
- the wiring tests in `test_attention_cmd.py::TestTheLauncherWaitsForASlowDaemon`
  and `test_hotkey.py::TestMaybeStartHotkey`

**The upload watchdog applies the same rule.** `launch.UploadServerSupervisor`
respawns a dead port at the cooldown rate, and the cooldown used to be the only
thing between a slow serve and a second one. A serve measured 4.7s to bind
against the e2e tier's 3s cooldown, the watchdog started another beside it, and
on Windows the second bind succeeded (`SO_REUSEADDR`, before "One port, one
server"): two live servers on one port, a pid file naming only the later one.
Now a serve the supervisor spawned
that is still alive inside `REGISTRATION_TIMEOUT_S` counts as starting, not
failed. Once it exits, or the window runs out, the cooldown decides alone as
before. The supervisor never ends that child either. At the default 60s cooldown
this guard never engages, because the cooldown check returns first; it matters
only when the cooldown is both below the 20s window and shorter than a serve's
startup (the e2e tier's 3s override is one). It is
not a cure either: a serve measured 28.65s to bind on a loaded desktop, past the
window, and was doubled all the same. The structural fix is the exclusive bind
("One port, one server" above): a duplicate now exits with `PortInUse`, and an
exited child never holds back a respawn. Pins:
`test_launch.py::TestUploadServerSupervisor`.

The detaching e2e tiers carry the other half of the lesson. A failed launch
must not leak what it started, so the tiers find it by a uuid argv marker, not
by learned pid (see CLAUDE.md, serve-watchdog tier).

### magent's stdout escapes rather than raises (2026-09-28)

A redirected Windows stdout (a pipe, a file, the Session-0 hand-off's
`out.txt`, the ssh channel `magent attach` reads) is the ANSI code page with a
handler that raises. One CJK or emoji project name ended a command with a
`UnicodeEncodeError` and rc 1, and `up` got that far only after its sessions
existed. The entry point (`cli/app.py::_escape_unencodable_output`) now gives
stdout one error handler, `magent.escape`. What the code page holds is written
byte-for-byte as before. A lone U+DC80..U+DCFF is written back as the byte
surrogateescape decoded it from, so a POSIX path that is not UTF-8 still prints
as that path. Everything else prints as its `\u4e2d` escape. stderr needs
nothing: Python already gives it backslashreplace.

Only the error handler changes, never the encoding: a parent that reads magent
in its locale encoding (the hand-off's `_read_handoff_text` falls back to
`mbcs`, a Python caller uses `text=True`) must keep getting that encoding. Only
a stream still on a born-with handler (`strict`, `surrogateescape`) is changed;
a handler somebody chose is kept. It is not `PYTHONIOENCODING` set for the
hand-off child, because every process that child spawns, the fleet included,
would inherit it, and magent's child-environment policy is strip-only
(`env.spawn_child_env`). Known limit: a UTF-16/32 stdout, which only an explicit
`PYTHONIOENCODING` gives, still raises on a lone U+DC80..U+DCFF. Pins:
`tests/unit/test_stdout_escape.py`, `tests/e2e/test_cli_flags.py`, and the real
hand-off runs in `tests/unit/test_desktop_handoff.py`.

### The bring-up never waits forever, and a probe with no answer is not an absent session (2026-09-29)

`WindowsPlatform.launch_psmux_session` is the one call that creates psmux
sessions, and every path reaches it through `psmux.launch_verified`: `--go`,
the menu's "u", and `magent up`, which is also the host side of `magent attach`.
It ran six client fan-outs: the has-session dedupe, kill-server, new-session,
send-keys, the send verifier's re-sends and the status-line decorations. Each
ended in a bare `p.wait()`. The wedge described in "Doctor names the wedge"
above makes every psmux control command hang forever. So one wedged socket held
the whole bring-up forever. It held every later wave with it, and over ssh the
attach's read of the host timed out with nothing to show for it.

**One deadline per fan-out.** Every wait is now `psmux.await_clients`: one
deadline for the whole set, not one per client. With a timeout per client, N
hung clients cost N budgets, which is the `_display_fan_out` lesson. A client
still running at the deadline is killed and reaped. The reap is itself bounded
(`_REAP_TIMEOUT_S`), because a timeout must not leave its own unbounded wait
behind. A client that exited before the deadline still hands over its code. The
clients are spawned with `DEVNULL` stdio, because `capture_output` is not a
bound on Windows (see the 90s answer to a 5s timeout above). Every budget is
per fan-out and has a measurement behind it:

- the dedupe and the kill-server get 30s each (`_DEDUPE_TIMEOUT_S` /
  `_CLEAR_TIMEOUT_S`). That is 1.5x the ~19s measured for one 46-socket
  has-session fan-out on a loaded host.
- new-session and send-keys get 60s per wave (`_CREATE_TIMEOUT_S` /
  `_SEND_TIMEOUT_S`). That is `upload_server.INJECT_TIMEOUT_S`, the
  product's one-attempt ceiling. A healthy new-session was measured at 892ms,
  and in the wedge it never finished. A send-keys against a busy socket was
  measured from 3s to past 70s. A send that is killed is never re-sent, because
  it may still have landed, and a second copy would type the command into a
  running agent. This is the paste's double-delivery law.
- the decorations get `SEND_KEYS_TIMEOUT_S` (20s). They are cosmetic.

The total is bounded, not fast: about 30 + 30 s, plus per wave 60 + 10
(panes-ready) + 60 s plus the verify.

**The dedupe has three answers.** `psmux.probe_sessions` answers `live`,
`absent` or `unknown`. `has_session` and `live_sessions` fold "never answered"
into "not live". That is the right fold for a status table. It is the wrong
fold for a bring-up, because there "not live" leads to kill-server and a fresh
new-session. Only `absent` (has-session answered rc 1, tmux's "no such session"
-- `psmux.HAS_SESSION_ABSENT_RC`) may lead to either. A probe that timed out,
could not even be spawned, or died with any other code (0xC0000142
STATUS_DLL_INIT_FAILED, an access violation, a signal, a usage error) is
`unknown`: a crashed client has said nothing about its session. That window
is refused with the reason "could not tell whether <name> is running". It gets
no kill-server, no new-session and no send-keys.

**Why unknown is never killed.** When the wedge was cleared, every session
probed alive. The sockets that had stopped answering were frozen LIVE agents,
not dead ones. A bring-up that reads silence as absence kills and re-creates
each of them. That is the mass restart "Doctor names the wedge" exists to talk a
human out of, done automatically, and it throws away every agent's running turn
and context. The two errors have different costs. A false "unknown" costs one
report line, and the next `up` asks again. A false "absent" destroys a live
agent. This is the same asymmetry as the idle rule above: when the state cannot
be proven, the bring-up does not act.

The same rule covers the other two waits:

- A kill-server that never answered means the old server may still hold the
  name, so nothing is created on top of it.
- A new-session that outran its wave is refused for that window alone. The rest
  of the wave and every later wave carry on, exactly as a psmux refusal (rc 1)
  already did.

**The reason reaches the human.** `launch_psmux_session` returns
`{name: reason}`. `launch_verified` merges it into its own report, and `bring_up`
returns `(created, {name: reason})`. All three bring-up surfaces (`--go`, the
menu's "u", and `magent up`, whose output `magent attach` relays from the host)
print their casualties through ONE helper, `launch.report_bring_up_casualties`:
the "N session(s) failed to come up" line, each known reason under it, dimmed,
then `launch.session0_note()`. The log hint ("on the host" for `up`) is its one
parameter. The three copies it replaced had already drifted.

**A refusal is final, even when the verify finds the session live.** Killing a
new-session client at its deadline need not stop the psmux server it already
forked (the server is a grandchild; that is why priority is a sweep). So psmux
can create the session late. The verify then reads it live, but nothing ever
typed its agent command into it. Counted as brought up, it is a bare shell under
a success line, and `--go` never revives. So `launch_verified` keeps every name
the platform refused in its report, whatever the verify reads. A refused name
that answers carries the platform's reason plus what the verify saw: it answers
now, it got no agent command, and `magent up` revives it. The same holds for a
dedupe or kill-server refusal whose session answers by the time of the verify.
This bring-up could not prove it, did not touch it, and says so rather than
claiming it.

`launch_verified` does not respawn a refused name that its verify also misses.
The respawn would only repeat the wait that failed, and on a wedged socket it
would double that wait. A name that is merely missing is still respawned. That
stays safe because the respawn goes back through the tri-state dedupe:
"unknown" is safe to ask about again, and not safe to kill, re-create or type
into.

Pins: `tests/unit/test_bringup_bounded_waits.py` runs against a real executable
named `psmux` on a tmp PATH. It records every argv and hangs the verb/name pairs
each test chooses. On Windows it dies with its `.cmd` launcher, so "killed" is
observable. The pins cover:

- the three answers;
- one budget for N hung clients;
- a dedupe hang, a kill-server hang and a stuck new-session, each through the
  real `WindowsPlatform` bring-up;
- a hung first send (sent once, never re-sent), a hung re-send (the last send,
  though the attempt cap allows a third) and a hung decoration. These run the
  send verifier's REAL `idle_sessions` verdict over a fake bare-shell pane;
- `launch_verified` reporting the unknown name with its reason, a refused name
  beside a respawned one, and a session created after its wait gave up.

They run at shrunk budgets. `TestTheProductionBudgets` pins the real ones. The
printer is pinned byte for byte on all three surfaces
(`test_the_casualty_block_is_byte_for_byte` in `test_attach.py`,
`test_status.py` and `test_launch.py`; written green before the three copies
were folded into one). The residuals are in the known-debt ledger.

### An auto node is chosen by its load history, and a recall is explicit (2026-09-24)

**Placement reads history, not a reading.** `"node": "auto"` is resolved by
`launch.place_node_projects`, its own phase between selection and launch, so a
dispatcher only ever sees a nick; `up` runs the same phase once before its
fan-out so one `up` spreads like `--go`, and an auto project it cannot place
fails in its own row with the placer's reason. The score is spec §11
over the sync daemon's last 30 minutes of samples: the p75 of `load1 / nproc`,
plus half of how far the window's peak rises above 1.5 times that p75, plus
half of how far the newest free memory falls below 15%, plus 0.05 per session
of ours; ties go by config order. A node under 10% free memory
(`MEM_HARD_FLOOR`) is not a candidate while another is above it. A node with
fewer than five samples in the window gets exactly one live `sample` call,
which is then its only sample, and none under `--dry-run` or a tile-only pass;
a node that does not answer it is left unscored. A single reading would place
a session on a box that happened to be idle for one second of a bursty minute.

**A placement sticks, and placing writes nothing.** A project stays on its
node until that node leaves `settings.nodes`; only then is it re-placed, with
the reason printed. The placement phase never writes `node-map.json`: the
bring-up records it once it has actually happened, so a failed
launch leaves nothing sticky behind, and `magent node plan` can render the
very same objects while writing nothing, pinned byte-for-byte. Nothing moves a
running session on its own; `magent node recall --to` is the only mover.

**The node decides what a plain re-up resumes.** A session brought up again
passes no resume id: `bring_up.sh` runs `claude --continue` over
the node's own transcripts, or the fresh form when there are none. The PC's
pulled copy can be one pull stale, and an explicit `--resume` has no fresh
fallback on a node that lost the file. `claude --resume <id>` is used only
where magent installed that conversation first: `recall --to` (installed
by `install_transcripts.sh`, under the name magent's one encoder
gives the node's own `realpath`; the node never encodes) and the resume
`recall --local` prints.

**Recall never races the daemon, and a pull it cannot finish stops it.** The
last pull goes through `node_sync.final_pull`, under the lock the daemon's tick
holds. The placement is cleared at the end of a recall and a cleared placement
is never pulled again, so a pull that a re-run could still complete stops the
recall before anything is stopped, installed or cleared: a node that answered
with an error, a pull that left files behind, a placement the pull no longer
found, or a node map another process holds busy or left torn all exit 1
with the project still placed and "run the recall again" -- except a pull
stuck at its mark, which a re-run would only meet again: that stop names
nodes.log, where both marks are, instead; a daemon still
holding the node past the wait exits 3 the same way. Only a node that does not
answer at all (ssh's own 255, or a timeout), or one this config cannot pull
from, is reported and not fatal, because no re-run helps: the last `repos.json`
record stands in for the live commit report, and the command that stops the
session is printed with its target single-quoted, `kill-session -t '=<sid>'`,
because zsh reads a bare `=word` as a command lookup. That command is one
`ssh <target> "…"` line only for a plain sid; any other sid gets two steps
(ssh, then run it on the node) with its `'` escaped, since the local shell
would expand `$(…)`, a backtick or `!` inside the double quotes. "Stopped" is
printed only when `remote_mux.kill_session` returned True; when the call
failed, recall says the session may still be running and prints that command.

**`--local` installs by the rules a `--to` send uses.** The local folder is
the one a launch opens, resolved by `launch._resolve_path` and never
`Path.resolve()`d, because Claude files a conversation under the path the
session was started in, link and all. The pulled mirror is copied by
`remote_mux.copy_mirror`, which shares its membership rule with the tar that
ships a mirror to a node: no link is followed, a mirror that is itself a link
is refused, and a pull's `.part` temp is never copied. A local file the node's
copy changed is named, because it may be work this PC had.

**Not yet measured (plan G Task 16, a user-run probe):** whether
`claude --resume <id>` resumes a conversation installed under another
folder's name. Until it is measured, when the conversation's id is known,
`recall --local` prints the exact command, `claude --resume <id>`, and
under it a `resume by hand` line: run `claude --resume` in the project's
folder and pick the conversation from the list; with no id it prints plain
`claude`. `recall --to` starts the moved session on the new node with
`claude --resume <id>`, or fresh when no conversation was pulled.

### A finished, long-idle agent is parked, not killed (2026-09-27)

Idle agents hold memory. Measured on this box: 31 agents held 57.6 GB of commit
(`claude.exe` alone 30.3 GB) on a machine at 151.5 of 253.7 GB, and stopping the
four sessions idle for more than two hours would have returned about 9.3 GB.
What an idle agent does NOT hold is anything its conversation needs: Claude Code
writes whole transcript records, and `claude --resume <sessionId>` in the same
pane picks the conversation back up (measured against a real Claude Code in
poc-reap2: the transcript byte-identical after the kill, the resumed agent
recalling its last reply).

**Decision: `magent serve` parks a session whose agent finished its turn and has
been idle past `settings.idleReap.afterMinutes`. It hard-kills the agent's
process tree, keeps everything else, and records the session as `parked`.**
`reap.py` splits the risky logic from the I/O: a pure core (`Signals` →
`decide` → `"reap"` or one of `VETO_REASONS`) tested with no processes and no
clock, and a thin gather/act layer (`gather`, `_read_one`, `_stop`, `_park`,
`sweep_once`) around it.

*The pane is kept; only the agent goes.* The pane's shell, the psmux session,
the window and its tile are untouched, and nothing above the agent root (the
`cmd /c` wrapper, the pane shell) is ever killed. After the kill the pane gets
one typed line, `Platform.pane_reset_command`: for PowerShell it turns off the
mouse, paste and keyboard modes the agent left on, pops the alternate screen,
clears, and prints a notice naming the exact resume command. A shell with no
reset line, or a re-walk that does not read the pane idle, leaves the dead frame
on screen with a WARNING; a resume repaints the whole frame anyway.

*Finished only* (the user's decision, 2026-09-27). A session is idle only when
its last turn ENDED. The three whitelists are record `done`/`idle` (R7), Claude
Code status `idle` (R6) and pane `idle`/`limit` (R9). Two meanings of "waiting
on the user" must not be confused here. `needs-input`, Claude Code's `waiting`
and an on-screen dialog are a turn BLOCKED mid-way on a question or a
permission prompt; killing one abandons the pending tool call, and on resume
Claude Code drops that turn rather than redo it. `done` is "your turn": the
turn is over and the user has not looked yet. Blocked is never parked;
your-turn is, which is why a parked `done` session loses its `[+]` title badge
and sinks to the bottom of `watch`. `working` is never parked either: the Stop
hook writes it while the `background_tasks` ledger still lists a subagent or a
shell.

*Every reading must agree, and unknown is never idle.* Ten rows (R1-R10) and 24
named reasons, cheapest first; the first failure spares the session, and a
session's reason is logged only when it changes. Two checks carry the design:

- **`record-stale`** (R7): the record's `ts` must not predate the agent root's
  creation time. A record older than the process was written for a previous
  agent in that directory, or by a hook that has since died (which is exactly
  what happened on this box for two months), and it says nothing about this
  agent.
- **The console-membership check**, the last stage of `psmux.idle_sessions`
  (see "An idle pane is proven, not read off the foreground"). An orphaned
  agent is outside the pane shell's parent-pid subtree but still on its
  console, so it would receive anything typed. The reset is typed only after
  `idle_sessions` re-walks the emptied pane and asks its console who is on it.

R10 re-reads every per-session row for the same agent pid and creation time just
before the stop, which closes the window between the sweep's first read and the
kill. At most `REAP_MAX_PER_SWEEP` (3) sessions are parked per sweep, oldest
quiet first, so an upstream change that makes every session read idle costs
three sessions, not the fleet.

*The kill is identity-guarded, and only what is proven is killed.* It is a hard
kill and never a clean `/exit`: typing into the pane is the dangerous verb (an
Enter can answer an open dialog, and a mangled `/exit` once reached the model as
a prompt), and a clean exit buys only housekeeping. `_stop` reads a
`procs.precise_filetime` bound, then a fresh snapshot, and keeps an entry only
if its identity reads, it was created before the bound (not a newcomer on a
reused pid), and it was created after its kept parent (Toolhelp never rewrites a
parent pid, so an older process listed under a pid is an adopted stranger). It
refuses the whole stop when the agent root's identity differs from the recorded
one, or the listed tree contains the pane pid or a psmux image. Each kill is
`procs.terminate_verified`: ONE handle opened with terminate and query rights,
the image and creation time re-read through it, and only then
`TerminateProcess`. The handle pins the process object, so the pid cannot be
reused between the check and the kill (pid reuse within seconds was measured
twice). Children die before their parents and the agent root last, followed by
one bounded straggler pass.

*The record is written LAST.* `parked` reaches `agent_state` only after the
agent root is confirmed dead. A stop that refused, or a root that outlived it,
writes nothing and types nothing, and the agent joins a per-process failed set,
so a live agent can never be marked parked. No SessionEnd hook runs on a hard
kill, so this write is the only record of the outcome. `parked` is additive:
`RECORD_VERSION` goes to 2, the key set and value types are unchanged, v1
writers (an older installed hook, Codex's `notify` recipe) stay valid, and the
resumed agent's SessionStart overwrites `parked` with `idle`.

*Resume is `--resume <id>`, and only when asked.* `psmux.revive_sessions` takes
`resume_parked`, and only `status`'s `r<n>` passes True. It types
`build_resume_command(tool, cmd, session_id)`, never `--continue`, and clears
the record once the send lands. The id is typed into a shell, so it must fully
match `sessions.live.SESSION_ID_RE`; a parked record without such an id is left
alone with a WARNING. A bulk revive (`magent up`, and the `up --json --revive`
that `magent attach` runs on the host) leaves a parked session alone, because
resuming on every attach would undo the saving.

*Owned by `serve`, like the other sweeps.* `upload_server._supervise_idle_reap`
is a daemon thread next to `_supervise_psmux_priority`, for the same reasons:
serve is effectively always up, and after the Session-0 hand-off it runs on the
desktop, where the fleet's processes can be opened and terminated. The process
gates (`reap.process_off_reason`: the env, psmux support, an interactive logon
session) are read once at startup. `settings.idleReap` is re-read every sweep,
so turning it on, or fixing a broken config, needs no restart. Each sweep runs
under `lockfile.exclusive_lock("idle-reaper")`, so two serves on different ports
never sweep at once. The cadence is `IDLE_REAP_INTERVAL_S` (300 s), the
threshold has a 30-minute floor (`reap.threshold_s`), and every age comes from
disk, so a serve restart neither delays nor hastens a park.

*The kill switch is the sharpest test-isolation law.* `MAGENT_IDLE_REAP=0` joins
`MAGENT_PSMUX_BOOST` and the two supervisor switches, and it outranks them: the
reaper is the only code in the product that TERMINATES processes it did not
spawn, reached by psmux session name and `~/.claude` session file, and a HOME
redirect contains neither. `tests/conftest.py` pins it off for every tier and
every child `env=` carries it. Two autouse kill guards sit under every test as
well: `reap._stop`'s default snapshot fails the test instead of walking the live
process table (`@pytest.mark.live_process_table` opts out), and every in-process
`procs.terminate_verified` refuses a process the test did not register in
`own_pids` -- by identity (pid, image, creation time), so a reused pid is a
stranger. Under it, the kernel32 `procs` hands out lets `TerminateProcess` land
only inside that guarded call and never lets `TerminateJobObject` land. The
kills no guard can wrap for every tier (`os.kill`, `taskkill`, psmux
`kill-server`, which e2e teardown uses on its own daemons) are pinned out of
reap's source instead. Unlike `MAGENT_PSMUX_BOOST`, an invalid `MAGENT_*` environment turns
the reaper OFF instead of falling back to the default: the one supervisor whose
verb is destructive fails closed.

*Doctor names the silent failure.* Reaping is on by default while the hooks are
opt-in, and with no records R7 spares every session forever. The `idle-reap`
check is WARN-at-worst: OK names the threshold (and an `afterMinutes` raised to
the floor) or the gate that is off, and it WARNs when reaping is on but the
state hook is not wired for UserPromptSubmit, Stop, Notification and
SessionStart.

Everything goes to `reap.log`: one INFO line per park (the session, its id, the
agent's identity, the idle age, the killed and survivor counts, `freed~<MB>`),
a session's reason whenever it changes, and a WARNING or ERROR for every
failure.

Pins: `tests/unit/test_reap_decide.py` (one test per veto, the strict age
boundary, NaN and unknown times), `test_reap_gather.py`, `test_reap_stop.py`
(the snapshot bound, adopted strangers, stragglers, the guards, and win32 kills
of processes each test spawns), `test_reap_park.py` (the order, nothing parked
unless the stop verified, the record last), `test_reap_sweep.py` (the gate,
order and cap, R10, the failed set), `test_reap_thread.py`,
`test_reap_resume.py`, `test_reap_isolation.py`, `test_sessions_live.py`,
`test_kill_guards.py` (the conftest guards and reap's reach, pinned by what
they do),
`test_platform_pane_reset.py`,
`tests/unit/test_procs.py::TestTerminateVerified` / `::TestConsoleClients` /
`::TestProcessIdentity`, `tests/unit/test_fleet.py::TestInputDraft`,
`tests/unit/test_doctor.py::TestCheckIdleReap` and
`tests/unit/test_state_hook.py::TestStateHookNeverWritesParked`. The
real-multiplexer tier, `tests/e2e/test_reap_real.py`, has run green only on
its POSIX leg; its Windows legs have not run yet (see Known debt).

### A node signs in on the subscription, and git on the gh login (2026-09-30)

The nodes design shipped the user scope but not the Claude login: "auth
transfer minus Claude login", with a manual `ssh <user>@<host> claude` as the
last step of every `node setup`, and a per-node GitHub ssh key that needed an
extra `gh` scope (`admin:public_key`) before it could be registered. Both were
manual steps per node. This replaces that decision: `magent node setup` hands a
node working Claude and GitHub access by itself, and the only thing a human
ever does is one browser approval, once per PC per year.

**Claude runs on the subscription, from a token minted once.** `claude
setup-token` is Claude Code's own "long-lived authentication token (requires
Claude subscription)". After one browser approval it prints an OAuth token
(`sk-ant-oat...`, one year) that bills to the Pro/Max subscription. It is not an
API key. `node_auth.ensure_token` runs it the first time a setup needs a token,
and at no other time. It keeps the token in `~/.magent/claude-oauth-token`
(0600 and this user's on POSIX; a protected single-ACE DACL set at CreateFile
time on Windows) and reuses it for every node and every later setup. It
re-mints only when the token is within `RENEW_BEFORE_S` of its year, when its
file cannot be trusted, or on `magent node auth refresh`, which is the repair
the doctor names when Anthropic rejects the token.

**Why not copy the login.** `~/.claude/.credentials.json` and every ccswap
slot hold a REFRESH token, and a refresh token rotates. The first machine to
refresh it invalidates the copy on every other machine, so a node refreshing a
copied chain would sign this PC out, along with every live session on it. A
setup-token token has no refresh chain to share, so one token can sit on any
number of nodes at once.

**Why never an API key.** An API key bills per token, not to the
subscription. Claude Code ranks an API key (or an `apiKeyHelper`) ABOVE an
OAuth token, so a stray `ANTHROPIC_API_KEY` in a node's login environment would
silently win. So setup-token runs under `env.claude_mint_env()` (no
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or older `CLAUDE_CODE_OAUTH_TOKEN`),
the payload refuses anything that is not an `sk-ant-oat` token, and node
sessions drop both key variables before the agent starts.

**The token reaches a session through ONE seam.** `remote_mux._login_argv`
builds every node pane's command, the resume and the fresh fallback alike. It
now runs `SESSION_AUTH_PRELUDE` first: unset `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`, then have the shell read
`~/.magent/claude-oauth-token` into `CLAUDE_CODE_OAUTH_TOKEN` and export it.
The shell reads the file, so the token is never in an argv, the tmux command
line or a log. The file gets there as the payload's owner-only
`claude-oauth-token` member; the manifest carries its digest, never the token.
`node_apply`'s `claude_auth` step installs it 0600 and masks it out of every
row, as it does the gh token. The node's own `~/.claude/.credentials.json` is
never touched. Nobody logs in on a node any more, so nobody clicks through
Claude Code's first-run screen there either. With a token in place,
`claude_onboarding` sets `hasCompletedOnboarding` in the node's
`~/.claude.json`; that is the only key it changes.

**The token never reaches a screen.** setup-token's stdout is read through a
pipe. `node_auth._Forwarder` passes its UI to the terminal only up to the first
`sk-ant-` and nothing after it, so the user sees the browser prompt and the
success line but never the secret. Errors and log lines quote lengths and exit
codes, never output. Tests pin that no token substring reaches captured
output, a log, a fake's argv or a doctor row. The fakes record credential
environment variables by sha256 only.

**The paste prompt reaches the person while setup-token waits.** Its
`Paste code here if prompted >` has no newline after it. The forwarder passes
it on as it arrives, but a reader that shows whole lines only (a PowerShell
`ForEach-Object`/`Tee-Object` pipeline, a pager) holds it until the next
newline. The first live `node setup` ran through such a pipeline, and the
prompt appeared only after the 900s mint had timed out. So when magent's own
stdout is not a terminal, `mint_token(line_buffered=True)` ends an unfinished
line once setup-token has been quiet for `QUIET_S`. It adds only a newline,
never a held byte, so a quiet stretch in the middle of `sk-ant-` still shows
nothing. setup-token's stdin stays this terminal's either way, and that is
where the pasted code goes. A timeout names the next step: run
`magent node auth refresh` in a terminal, not through a pipe.

**GitHub needs no key and no new scope.** A node already gets this PC's gh
login. `node_apply`'s `git` step makes it git's `github.com` credential helper
(`gh auth setup-git`, put back if it went missing). It also adds
`url.https://github.com/.insteadOf` for `git@github.com:` and
`ssh://git@github.com/` to the node user's global git config, so an ssh remote
is fetched and pushed over https through gh. The per-node ssh key becomes
optional. `register_ssh_key` tries it only when this PC's gh already holds
`admin:public_key`. Every other case is a `skip` ("not needed: git uses the gh
login"), never a failure, and setup never asks for `gh auth refresh`.

**The doctor proves both, and names the repair.**

- `claude-auth`: the token file is a plain, owner-only file holding a
  subscription token. `claude auth status --json`, run with the token
  exported and the key variables dropped, reports `authMethod` `oauth_token`;
  an `apiKeySource` or `api_key*` method is a fail, because that key would
  win. Then a `count_tokens` call with the token on curl's stdin checks that
  Anthropic still accepts it. No model runs, so nothing is billed. A 401, or a
  403 that says "revoked", fails with `magent node auth refresh`.
- `github`: `git credential fill` for `github.com`, asked exactly as a clone
  would ask. Both rewrites must be present. Then GitHub's `/user`, with the
  credential on curl's stdin, must answer 200.

No ssh to github.com is involved any more, so the doctor writes nothing, not
even known_hosts.

**What was verified, and what is assumed.** Verified on Claude Code 2.1.284:
`setup-token`'s help text; `auth status --json` reports `oauth_token` for a
`CLAUDE_CODE_OAUTH_TOKEN`, `none` without one, and `api_key` with
`apiKeySource` when `ANTHROPIC_API_KEY` is also set (checked with decoy values
under a throwaway home). Assumed: that `count_tokens` accepts a setup-token
bearer with the `oauth-2025-04-20` beta and bills nothing, and that
`hasCompletedOnboarding` is the only first-run gate on a headless node. The
live `magent node setup` is where both are first proven.

Pins: `tests/unit/test_node_auth.py` (mint once, reuse, renewal, force,
forwarder, file modes and DACL), `tests/unit/test_node_apply.py`
(`TestTheClaudeSubscriptionToken`, `TestNoFirstRunScreenStandsBeforeTheToken`,
`TestGitUsesTheGhLogin`), `tests/unit/test_remote_mux.py`
(`TestANodeSessionStartsOnTheSubscription`, run under real bash),
`tests/unit/test_node_provision.py` (the doctor's two checks under real bash,
the optional key, the PC's token shipping) and `tests/unit/test_node_cmd.py`
(`TestNodeSetupMintsTheClaudeTokenOnce`, `TestNodeAuth`).

### A node joins in three commands, and nothing after them needs a hand (2026-09-30)

The product rule: a person who installs magent and has root ssh to a machine
gets a project running there without an AI agent, or a human, finishing any
step for them. The documented path is three commands, `magent node add
<host>`, `magent config add <path> --node auto` and `magent up`, plus one
browser Approve per PC. Five pieces make that true.

**`node add` is setup.** It writes `settings.nodes` through the raw round-trip
(creating the config when there is none) and then runs `node setup`'s whole
flow inline (`node_cmd.run_setup`, which `node setup`, `node add` and a
bring-up's inline setup all call). The nick is derived from the host with the
same rule config load applies, so a derived nick always loads. Everything
that can refuse (the host shape, the public key, the user name) is checked
before anything is written. `node remove` edits config only, and refuses
while the node map places a session there: a removed node with a live session
would be a session nobody can reach. A project the removal would strand
(pinned to that nick, or `auto` with no node left) is switched to run on this
PC in the same save: asked at a person's console (default yes), `--local`
anywhere else, and otherwise refused naming the projects and that one
command -- never the validator's own words.

**A bring-up never walks a project onto a node that cannot run it.**
`node_onboard.ready_gate` runs before `up` and `--go`. Readiness is the
placement read (`nodes.placement_samples`: the load window, one live reading
for a thin node), not a second probe, so "ready" and "placeable" cannot
disagree. A node is not ready when it does not answer, or when it has no
Claude token while this PC has none to give it; a tokenless node is ready when
this PC holds one, because the bring-up's own provision ships it. At a
terminal the gate asks once and runs setup inline. Anywhere else it asks
nothing, mints nothing, sends nothing, and turns that project off for this run
in one line naming the fix. A daemon or the `up` that `magent attach` runs
over ssh must never block on a question nobody sees, and must never start a
browser approval nobody can click; an AST pin keeps every mint behind
`node_cmd._can_approve()`.

**"A person is here" is ONE check, and it is not `isatty`.** On Windows NUL
is a character device, so `sys.stdin.isatty()` is True under `< NUL`,
`stdin=DEVNULL`, Task Scheduler and every detached child magent spawns.
Measured: `magent node add` run with stdin=NUL started `claude setup-token`
and opened a browser on the desktop. `console.human_at_console()` is isatty
AND, on win32, `GetConsoleMode` on the stdin handle (NUL, pipes and files
fail it). `_can_approve`, and through it the ready gate, the renewal offer,
`node auth refresh` and the GitHub login offer, ask it; so does the picker's
raw-mode gate, which had the same trap and the same fix already. An AST pin
allows `sys.stdin.isatty()` in `console.py` and in `cli/app.py`'s two older
first-run/menu gates only. Those two pick a line-based prompt that reads EOF
and exits under NUL; nothing is started for a person there.

**No GitHub login is offered, not printed.** A node's git clones over https
with this PC's gh login, which provision shares. With no login, setup used to
print `gh auth login` and the node's first private clone then failed: a step
for the user. At a person's console, setup now asks once (default yes) and
runs `gh auth login --web --git-protocol https` in that terminal
(`remote_mux.login_gh`, the one gh call that is not bounded by
`GH_TIMEOUT_S`: a person is reading a code), then goes on and provisions with
it. Anywhere else it prints one `gh-login` line and asks nothing, and the
github-key rows, which would only repeat it, are not run. A rejected token
that comes from `$GH_TOKEN` is not offered a login, because no login can
replace it.

**The token has a lifecycle the person sees before it bites.**
`node_auth.token_health()` is the one reader. From `WARN_BEFORE_S` (30 days)
before the token's year ends, `status`, `doctor` and the node commands say so
in one line, and at a terminal the node commands offer the renewal: one forced
mint, then `push_token` provisions every configured node. A node that does not
answer gets the new token at its next bring-up, which always provisions. The
state is additive in `status --json` (`claude_token`) and never part of the
0/1/3 verdict: an ageing token is advice, not degradation.

**A pin is validated as a whole config.** `config add --node` and `config set
<p> node` run the resulting text through the validator `load_config` uses, and
write nothing on refusal, so a pin can never leave a config that no longer
loads.

**The sync daemon stops when nothing is placed.** The brief allowed two
lifetimes: exit once idle, or be owned by the attention daemon. Idle exit won,
because serve already owns the daemon's START (`_supervise_node_sync` ->
`launch.ensure_node_sync`) and serve is up on a real box far more often than
`attention -d`. Moving ownership would have left the daemon unsupervised
whenever attention was down. So `node_sync.expected(config)` (wanted AND
something placed in the node map) is the one question serve's spawn,
`status` and `node doctor` all ask. `run_sync_loop` exits `IDLE_EXIT_S` (10
minutes) after the map stops placing a session, and the next placement makes
serve start it again. A daemon started by `magent --config <file> up`
re-reads THAT file: it stops once the file is missing on
`GONE_AFTER_MISSES` consecutive reads, records the file it follows in
`~/.magent/node-sync.config`, and `status` names it when it is not the config
`status` read. `ensure_node_sync` still never re-aims a live daemon; a
deliberate `--config` bring-up must not be fought by a serve reading another
file. No new supervisor and no new env var, so no new test-isolation opt-out:
`MAGENT_NODE_SYNC` still gates serve's spawn.

**The folder is trusted before the agent starts.** Claude Code asks the first
time it starts in a folder, and its default answer exits; nobody is at a
node's pane to answer. bring_up.sh runs `node_apply.trust_main` after the
ship and before the session starts, which sets only
`projects[<folder>].hasTrustDialogAccepted` in the node's `~/.claude.json`
(its physical name too when it differs). A folder at or above the node user's
home is refused, since Claude trusts every folder below a trusted one.

A node's own statusLine is part of the same rule. A node with no
`statusLine` gets magent's `~/.magent/bin/statusline.py` (python3, stdlib),
which prints the `<Model> · <effort>` footer `fleet.parse_footer` reads, so
`sessions --json` shows a node session's model like a local one. A node's
existing statusLine is never replaced.

Pins: `tests/unit/test_node_zero_hands.py` (the three commands, whole, with no
input: one mint, the token in no argv and no screen, only in the 0600 payload
member), `tests/unit/test_node_onboard.py`, `tests/unit/test_node_ready_gate.py`,
`tests/unit/test_node_token_renewal.py`, `tests/unit/test_config_node_pin.py`,
`tests/unit/test_node_sync_lifetime.py`, and `tests/e2e/test_nodes_real.py`
D7 (the session starts on the node, the folder trusted).

## 3. Known debt

Ordered roughly by how likely a future change is to collide with it.

**The three-command path is proven whole only over fakes (2026-09-30):**
`tests/unit/test_node_zero_hands.py` runs `node add`, `config add --node
auto` and `up` end to end through the real CLI, but the machines are THE fake
ssh, gh and claude. The nodes_real e2e tier proves the node half on a real
sshd (the session starts, the token arrives by file, the folder is trusted),
but cannot run the product's own `node add`: without `--user` the node user is
this PC's login name, which on a CI runner is the runner's own account, and
setup.sh would run as root on the runner (packages, users, sshd drop-ins) with
nothing the rig's stamp-guarded teardown could undo. A real mint needs a
browser. Closing it needs a disposable node (a container or VM the job owns
whole) where setup may run as root unguarded.

**A node session keeps the token it started with (2026-09-30):** the
session prelude reads `~/.magent/claude-oauth-token` once, when the pane
starts. After `magent node auth refresh` the next bring-up ships the new token,
but a session that was already running keeps the old one in its environment
until it restarts. A shell a human opens by hand on the node (a new tmux
window, a plain ssh login) gets no token at all; a `claude` started there uses
the node's own login, if it has one. Both are left as they are: re-exporting
into live agents would mean typing into them, and the fleet's panes are the
only place magent starts claude.

**`node setup` keeps one node-key edge (2026-09-27; a second, the dangling
`.pub`, closed 2026-09-28):** `setup.sh`'s `user_node_key` refuses a symlinked
`~/.ssh` or `~/.ssh/id_ed25519` and makes the key 0600 on every run (F-ACL-1: a
default ACL overrides the umask, so ssh-keygen can leave a new key 0644). One
edge of that is left as it is on purpose.

*Closed: a dangling `.pub` link is refused, not written through.* The derive
branch's `> "$id.pub"` and ssh-keygen's own `.pub` write at generation run
whenever no `.pub` resolves, so a dangling link there used to have its target
created. `user_node_key` now refuses a `.pub` that is a link and does not
resolve (`[ -h "$id.pub" ] && [ ! -e "$id.pub" ]`) with its own fail row and
rc 1, before either write. The key's chmod still runs first, and the row
carries its repair note. Only the dangling case is refused, so the trade this
entry first named (adding `$id.pub` to the symlink refusal would fail a live
link too) was never forced: a live symlinked `.pub` is only read and stays
`skip` + `key`. Pins: `tests/unit/test_node_provision.py::
TestSetupShUnderRealBash::test_a_dangling_node_key_pub_is_never_written_through`
and `::test_a_live_symlinked_node_key_pub_is_only_read`.

*A dotfile-managed key now fails setup.* A node whose `~/.ssh/id_ed25519` is a
symlink to a 0600 key (a dotfile manager's layout) went `skip` + `key` before
F-ACL-1 and now fails every `node setup` with rc 1. That is deliberate: the
key's chmod runs on every run and is never done through a link, the rule
`user_authorized` already applies to `authorized_keys`. The fail row names the
symlink, so the user knows what to change. Pin: `TestSetupShUnderRealBash::
test_a_symlinked_node_key_never_reaches_its_target`.

**A failed `.pub` derive deletes a `.pub` link to a directory (2026-09-29):**
the dangling-`.pub` refusal above leaves a `.pub` link that resolves alone,
and a link to a directory does resolve, so it is not refused. `[ -f ]` is
false for it, so with a private key present `user_node_key` takes the derive
branch: `ssh-keygen -y ... > "$id.pub"` fails on the directory, and the
cleanup `rm -f -- "$id.pub"` then removes the user's link (never the
directory it points at) before the fail row. Pre-existing, the same at rc1,
and left unpinned: the refusal's `[ ! -e ]` is what the spec asked for, and
the mutant that would tell it from `[ ! -f ]` (R8) survives on exactly this
case. The fix, if it matters, is to refuse any `.pub` link that is not a
regular file, before the derive, the way the dangling one is.

**`stop_daemon` can kill a stranger named by a stale pid file (2026-09-29):**
`node_sync.stop_daemon` kills only while the daemon's lock is held, and kills
the pid the pid file names if that pid is alive. A daemon that died without
its own cleanup (a crash, a forced kill) leaves its pid file behind, and a new
daemon that has taken the lock but not yet written its pid is paired with that
file. If the OS has handed the old number to another process by then,
`daemon_pid()` reads it as live and the stop kills that process. The window is the few
instructions between `run_sync_loop`'s lock and its pid write. Pre-existing;
the wait for a late pid (2026-09-29) reads the same file first and neither
widens nor narrows it. The fix would be a pid written under the lock with
something only the daemon knows (its lock-time stamp, or its start time
checked against the process's), not a liveness check.
**Two Session-0 hand-off failures the launcher cannot describe (2026-09-29):**
both sit before or outside the Python launcher, so its fast, worded answers
cannot cover them.

- *A missing interpreter reads as "never started".* If the `sys.executable`
  that `run.ps1` names is gone by the time the task runs (a venv deleted or an
  upgrade that swapped the interpreter between staging and `/Run`), its `&`
  fails under `$ErrorActionPreference = 'Stop'` and PowerShell exits before
  any launcher exists. That leaves no `pid.txt`, no `rc.txt`, and a task that
  is not running, so after `_HANDOFF_START_GRACE_S` (30s) the caller hears
  "Task Scheduler never started the hand-off (is anyone logged on?)". The
  outcome, nothing brought up and `rc=None`, is right. The wording and the 30s
  wait are not, because only the launcher writes the immediate rc 1 with a
  reason, and here it never runs. Closing this would need a `catch` in
  `run.ps1` that writes `err.txt` and `rc.txt` itself, which makes PowerShell a
  writer again.
- *A crash of `launch.py` itself leaves no trace in the scratch dir.* The
  launcher handles a command that cannot start. Anything else it raises (an
  `rc.txt` still held after the record retries, say) goes to the task's
  hidden console as a traceback, and that console is gone when the task
  ends. The caller hears "exited without an exit code", or "never started",
  and the scratch directory it names holds nothing about why. After a pid
  record that failed (skipped by design) there is no pid to watch, so a crash
  past the one start check reads as the budget running out -- "may still be
  running" -- instead. Closing this would mean wrapping `main()` in a
  `try/except BaseException` that writes the traceback into a scratch file.

**Typed text cannot be delivered through a nested ConPTY over `ssh -t`
(2026-08-18):** `tests/e2e/test_ssh_real.py::test_typed_text_survives_a_real
_reconnect` is a loud `::warning` skip on win32. The test drives the real
supervisor under a pywinpty pseudoconsole, over a real `ssh -t`, into the
Windows sshd's own pseudoconsole — two ConPTYs in series. The typed text
arrives fine and is echoed by the remote; the ENTER does not. It arrives as a
literal win32-input-mode key record (`ESC [ 13 ; 28 ; 13 ; 1 ; 0 ; 1 _`, i.e.
VK_RETURN/CR — the same stream carries the `ESC [ ? 9001 h` DECSET that enables
that mode), so the remote's `readline()` never completes. Measured, with the
transcript quoted in the skip helper's docstring — and only measurable once
`_pty.expect` grew a real deadline, because before that the leg simply hung
until GitHub cancelled the job.

Not a product defect and not a user-visible one: a real attach pane is hosted
by Windows Terminal's ConPTY, where the keystroke arrives as a keystroke (the
CI-only `interaction` tier drives real `SendInput` chords through it). The
guarantee this test exists for is still covered on Windows by
`tests/e2e/test_pty_attach_status.py`, which pins the local rendering half cell
by cell, and the test itself runs for real on ubuntu and macOS. Worth trying
next: pywinpty's WinPTY back end (`PtyProcess.spawn(backend=Backend.WinPTY)`),
which predates win32-input-mode and may pass the CR through unencoded.

**Auto placement never rebalances (2026-09-24):** an `auto` project stays on
the node it was placed on until that node leaves `settings.nodes`. A node that
grows busy keeps its sessions; moving one is a manual
`magent node recall <project> --to <nick>`. Deliberate: a
move stops a live session and ships its conversation, which is not something
to do behind the user's back. If it bites, the fix is a `node plan` hint
naming the better node, never an automatic move.

**Recall moves Claude Code conversations only (2026-09-24):** `magent node
recall` refuses a project whose tool is not `claude` (exit 2). Codex has no
transcript layout magent pulls, and no resume-by-id form recall could print.
Its sessions come home by git alone: commit and push on the node, pull here.

**A bring-up's repo record knows the commit, not the branch (2026-09-24):**
`repos.json` written at bring-up carries each repo's sha from
`bring_up.sh`, with an empty branch and an unknown unpushed count, because the
bring-up reports only commits. A recall from a node that no longer answers
therefore prints the last known sha without a branch. A recall from a node
that answers records the full `repo_status.sh` report and replaces it.
**What the bounded bring-up still leaves open (2026-09-29):** there are five
residuals of "The bring-up never waits forever" in §2. Items 1-3 and 5 were
found by reading the code, and none has been seen on the fleet. Item 4 was
reproduced.
(A sixth, "a timed-out new-session may still produce its session and be
counted created", is closed: a refusal is now final, see §2.)

1. *Four helpers on the bring-up path are bounded only on paper on Windows.*
   `has_session`, `kill_server`, `send_keys` and `capture_pane` still use
   `subprocess.run(capture_output=True, timeout=…)`. That is the shape measured
   answering a 5s timeout in 90s ("Doctor names the wedge", §2), because
   `communicate()` waits on pipes a grandchild still holds. The bring-up reaches
   `capture_pane` through `_wait_for_panes_ready`, which makes one call per
   window, serially. Its 10s batch deadline is checked only between calls, so
   it cannot cut one short. It reaches `has_session` through
   `launch_verified`'s creation probe (`_CREATE_PROBE_TIMEOUT_S`, 3s, 16
   workers). A wedged socket that leaves a
   grandchild behind can hold each such call for as long as the grandchild
   lives. The fix is the one `probe_control_plane` took: discard what is not
   read, and bound what is read without `communicate()`.
2. *`live_sessions` with a `timeout` pays it once per probe, in turn.*
   `_probe_live` waits `proc.wait(timeout=timeout)` client by client, so N hung
   probes cost N x timeout. It `kill()`s a timed-out probe without reaping it.
   The default (`timeout=None`, unbounded) is deliberate for status, down and
   the picker, because a slow server (~19s for 46 sockets) must not read dead.
   So this only bites a caller that passes a timeout. `await_clients` is the
   drop-in shape.
3. *A spawn that fails partway through a fan-out leaks the clients already
   spawned, and can lose the call's refusals.* Every fan-out in
   `launch_psmux_session` spawns in a list comprehension: `spawn_unjobbed` for
   new-session, `subprocess.Popen` for send-keys, the re-sends and the
   decorations. When spawn k raises `OSError`, clients 1..k-1 are never passed
   to `await_clients`. They are not killed, reaped or waited on, and on a
   wedged socket they live as long as it does. The re-send and decoration
   spawns catch the error, so only those clients leak. The new-session and
   send-keys spawns do not catch it. The error escapes `launch_psmux_session`
   and discards the `{name: reason}` of every earlier wave. `launch_verified`
   logs the exception and its verify then sees those names as merely missing,
   so it respawns them. That is safe, because the respawn goes back through the
   tri-state dedupe, but a refusal's reason is lost and its wait is paid twice.
   The shape of the fix is to spawn into a list that is awaited in a `finally`,
   and to return the refusals gathered so far.
4. *The pins' fake `psmux.cmd` breaks under a non-ASCII temp root.* The fake
   in `tests/unit/test_bringup_bounded_waits.py` is a `.cmd` whose one line
   names the interpreter and the script by absolute path, and it is written as
   UTF-8. cmd.exe reads a batch file in the OEM code page. On a code-page-437
   box, a script in a directory named `prøbe-т` was looked up as `pr├╕be-╤é`, so
   the interpreter found no script (rc 2). Every win32 pin in that module would
   then fail, and the cause is the harness, not the product. The fix is the
   Session-0 run.ps1 lesson (§2): do not let a script's bytes be re-read in
   another code page. Either write the `.cmd` in the OEM code page, or keep
   every path in it ASCII by passing the script path through the environment.
   This belongs with the 3.19.4 shim-encoding item.
5. *A killed send-keys leaves a bare shell counted "Brought up".* A first
   send-keys (or a re-send) that gives no answer within `_SEND_TIMEOUT_S` is
   killed and never sent again, because it may still have landed. Its window
   is then left out of `_verify_sends_landed`, and nothing reaches
   `launch_psmux_session`'s refusals: the only trace is a WARNING in
   `launch.log`. `launch_verified` finds the session live, so it is counted
   created, possibly with no agent running. That is the shape the late-created
   session had before a refusal became final. The NEXT `magent up` revives
   such a pane; the run that created it does not, because `up` only revives
   sessions that were live before it began, and `--go` never revives. Refusing
   it would be wrong too, since the command may be running. The fix is a third
   outcome: return the unsure names beside the refusals, and have
   `report_bring_up_casualties` name them on their own line ("may not have its
   agent") without counting them as failed.

**Attach-pane reconnect is only reachable from a Windows client (2026-08-09):**
`attach_client.py` itself is OS-agnostic (stdlib + click; the `Popen` in
`_run_ssh` inherits the console on POSIX exactly as it does on Windows) and its
unit tier runs everywhere, but the only code that spawns it is
`attach_client.py::spawn_attach_window`, which opens `wt` windows. There is no
macOS/Linux client window-spawn path for remote attach to wire it into — a
pre-existing gap this change neither widens nor closes. A future POSIX attach
client should call `attach_client.pane_command` as-is. Related and narrower:
the corpse scan recognizes the supervisor by its Windows executable name
(`magent-attach-client.exe`), which is fine because `process_cmdlines` is
Windows-only today; a POSIX process scan would need the extensionless name
added.

**Title badges are ambient state, not guaranteed state (2026-07-07, narrowed
2026-08-15):** the attention daemon's `BadgeRenderer` rewrites window titles via
`SetWindowTextW`, but shells/terminals with their own title logic (OSC 0/2
sequences, Windows Terminal tab-title settings) can overwrite a badge at any
time. The flash (and toast/ntfy when enabled) are the *reliable* signals; the
badge is best-effort ambience.

*Narrowed:* a title overwritten out of the `magent:` grammar is now repaired on
the next daemon tick (see "Window titles are magent's, not the app's"), so the
loss is bounded by the poll interval rather than permanent — but only while the
attention daemon runs, only on Windows, and only for sessions still live in the
agent-state store. With no daemon there is no repair, and the spawn-side lock is
the whole defense.

**POSIX terminals with no title lock (2026-08-15):** gnome-terminal's `--title`
is deprecated and VTE yields to the application's title; konsole's title format
is a profile-only setting; Terminal.app's `custom title` and iTerm's session
`name` are the stickiest channels those apps expose but the *displayed* title is
still composed per profile. So on those four emulators a program in the pane can
still rename a magent window out of the grammar — and unlike Windows there is no
repair, because neither POSIX backend implements `supports_attention_signals()`,
so `BadgeRenderer` never runs there. kitty (both OSes), alacritty and xterm ARE
locked. Closing this properly means either a POSIX attention backend (wmctrl /
System Events retitling) or shipping per-emulator profile config, neither of
which the current fleet needs.

**CI multi-monitor emulation is unavailable on the SHARED legs (R4-05 →
partially closed on Windows + user-topology replay, 2026-07-15):** hosted
GitHub runners do not
materialize `xrandr --setmonitor` VIRTUAL monitors under Xvfb, so the
platform/e2e CI legs exercise windowing against a single screen;
`setup-virtual-displays` emits a loud `::warning` when this happens instead of
pretending otherwise.

*What is now closed (Windows):* the dedicated `monitor-lab` CI job
(windows-latest, not a required check) installs the parsec-vdd virtual-display
driver and fabricates a mixed-DPI, multi-monitor topology in-process, then
drives magent's REAL `--go` launch+tile pipeline across it and asserts each
window rect lands in its `compute_grid` cell in physical pixels
(`tests/platform/test_monitor_lab_tiling.py`, engine in
`tests/platform/monitor_lab.py`). The offline grid-math layer is pinned
everywhere by `tests/unit/test_monitor_lab_topologies.py`, which feeds
committed golden topologies (`tests/platform/fixtures/topologies/*.json`) into
`compute_grid` and locks the mixed-DPI slot arithmetic + per-monitor
column-collapse. `FakePlatform` unit tests still cover the placement logic on
every OS/leg.

*Replaying a user's monitor topology:* `magent doctor --json` emits the
live topology under a top-level `monitors` key (list of `grid.MonitorRect`
fields — `x/y/w/h/is_primary/scale_factor`), so a bug report can hand us the
reporter's exact setup. `tests/platform/doctor_replay.py` (a pure, POSIX-safe
planner) parses that blob and maps each monitor to the closest resolution+DPI
the lab can physically achieve — snapping `scale_factor` to a standard Windows
step and capping it where the effective resolution would fall below the OS
~1024×768 floor (e.g. 720p can't exceed 100%). Every divergence is recorded as
a deviation, never silently approximated. The live tier
(`tests/platform/test_doctor_replay.py`, same `monitor_lab` gate, sharing the
one session-scoped `lab` fixture in `tests/platform/conftest.py` so the driver
still installs once) materializes each committed sample report
(`tests/platform/fixtures/doctor_reports/*.json`) and runs the same real `--go`
tiling assertion. One thing the live lab does NOT reproduce: exact report
*origins* — the runner's own primary is immovable, so displays are replayed
left-to-right to its right, not at a report's negative-x "left-of-primary"
coordinates. That origin/arrangement math (the classic tiling bug class) is
instead pinned OFFLINE by `tests/unit/test_doctor_replay_offline.py`, which
feeds the same parsed reports through `compute_grid` against committed golden
slots — negative origins included — and runs everywhere in the unit gate.

*What remains open:* macOS has no equivalent virtual-display lab (no
parsec-vdd analogue wired up), and the Linux/RANDR emulation path under Xvfb is
still unmaterialized — a real multi-monitor story on those two platforms
(self-hosted runner or a working RANDR emulation) is future work; do not build
it in the Windows tier.

**macOS has no window-over-SSH e2e coverage (documented limitation,
2026-07-10):** the real-SSH e2e tier (`tests/e2e/test_ssh_real.py`, over the
live loopback sshd `.github/actions/setup-ssh-server` provisions) proves the
non-interactive attach control channel (`ssh <target> "magent up --json"`)
on all three OSes, the full `magent attach` workflow — remote bring-up,
psmux-session survival past the ssh session, real `wt` windows, tiling,
`serve --ensure` survivor — on Windows, and launch.py's nested remote quoting
(`xterm → ssh -t → bash -lc 'cd … && cmd'`) on Linux. macOS window legs emit
a `::warning` and skip, for two stacked reasons: Terminal automation is
TCC-blocked on hosted runners (same wall as the tests/platform macOS render
leg), and `platform/macos.py::launch_terminal`'s ssh branch embeds the
`ssh -t … "…"` string — double quotes and all — inside an AppleScript
`do script "…"` literal, which is unverified on real hardware and looks
quoting-hostile. Verifying (and, if broken, fixing) the macOS
ssh+Terminal.app path needs a real Mac; until then the skip is loud, never a
green pass. The macOS `setup-ssh-server` step exports `MDTEST_SSH_HOST` only
when its wire smoke actually passes, so a flaky hosted-runner sshd degrades
to a loud skip of the real-wire tests instead of a red job (the dry-run ssh
tests keep running either way).

**Real-PTY menu coverage + real-browser upload coverage (2026-07-15):** two
tiers close the "we only ever tested this through a fake terminal / at the
socket layer" gaps.

*Real-PTY interactive menu (`tests/e2e/test_pty_menu.py`, marker `pty`):* every
prior test of the no-subcommand interactive path went through Click's
`CliRunner`, where `sys.stdin.isatty()` is False — so the real first-run/menu
branch in `cli/app.py` never executed the way a user hits it. These tests drive
the installed `python -m magent` under a GENUINE pseudo-terminal (pexpect on
POSIX via `os.forkpty`, pywinpty/ConPTY on Windows; the uniform driver is
`tests/e2e/_pty.py`), assert on the plain on-screen text (escape sequences
stripped first, children launched `NO_COLOR=1` so click emits none of its own),
and — for first run — assert the VALID config the wizard writes to disk. They
ride the existing `end-to-end` CI job on all three OSes (pywinpty is a
win32-only marker in the `dev` extra; nothing else changes). No honest gap:
this is the real terminal, all three flows (seeded-discovery first run, menu
render + quit, group-submenu round-trip).

*Real-browser upload (`tests/e2e/test_upload_browser.py`, marker `browser`,
dedicated `browser-upload` ubuntu job, CI-only, gated on `MDTEST_BROWSER=1` +
present Playwright/chromium):* a real headless Chromium loads the real mobile
upload page served by a real `magent serve` on loopback, performs the real
gesture (tap pill → attach a real PNG → the page's own `fetch('/upload')`
fires), and the test asserts the bytes the product writes to
`~/.magent/uploads` are byte-identical to what was attached, plus the page's
title/form contract so a template regression fails loudly. *Honest gap (small,
deliberate):* hosted Linux runners have no `psmux` binary and `LinuxPlatform`
does not implement `launch_psmux_session`, so the test puts a `psmux` on PATH
that execs real `tmux` (a sh wrapper since 2026-09-25, so it can record each
`send-keys` argv and a test can count pastes) and stands up a real detached
`tmux` session on a private socket
(`TMUX_TMPDIR` confined to tmp). Session discovery, upload validation, AND the
`send-keys` injection therefore all exercise a genuinely live multiplexer — the
only substitution is the multiplexer *binary's name*, and the deliverable under
test (the file transfer + on-disk write) is 100% real. The upload server's
no-token loopback bind is exercised as-is; no auth was added.

**Real-tailnet coverage (2026-07-15):** `tailnet.py` (`ip4` / `magicdns_host`
/ `probe` — the single owner of every `tailscale` CLI probe) and the
Tailscale-facing half of `magent serve`'s default bind
(`upload_server._bind_addresses`) had only ever been unit-mocked: no test had
ever run the real `tailscale` binary or proven the server listens on the
machine's Tailscale IPv4. `tests/e2e/test_tailnet_real.py` (marker
`needs_tailscale`, CI-only, gated on `MDTEST_TAILSCALE=1`) closes that gap
against a REAL node: the dedicated, non-required `tailnet` ubuntu job joins an
ephemeral, tag-scoped Tailscale node via `tailscale/github-action` (SHA-pinned,
OAuth + `tag:ci`), and the tests assert `tailnet.ip4()` equals real `tailscale
ip -4` (and is a genuine 100.64.0.0/10 CGNAT address), `probe()` reports the
live node, `magicdns_host()` equals real `tailscale status --json`
`Self.DNSName`, `_bind_addresses(None)` is exactly `["127.0.0.1", <ts ip>]`
(never the wildcard), and a real `magent serve` with no `--host` answers
`/health` on both loopback and the Tailscale IP while leaving the LAN wildcard
provably unbound (a fresh socket still binds the runner's own LAN IP on that
port). *Deliberate operational note:* the OAuth secrets
(`TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET`) are user-side setup that does not
exist yet — the job detects their absence and skips **loudly** (`::warning`,
job stays green) so forks and pre-setup PRs are never failed and a skip is
never mistaken for real coverage; once the secrets (plus an ACL `tag:ci`
stanza) are added it goes fully live with zero code change. Locally the tier
skips (no `MDTEST_TAILSCALE`, no live node) — nobody joins a tailnet on a dev
box to run it, the same never-on-a-dev-box posture as `needs_ssh` /
`monitor_lab`. The upload server's no-token loopback+tailnet bind is exercised
as-is; no auth was added and the default bind logic was not touched.

**Long-run daemon-stability soak (2026-07-15):** closes the "every daemon test
runs for seconds — no soak / long-run stability" honesty gap. `tests/e2e/test_soak.py`
(marker `soak`, CI-only, gated on `MDTEST_SOAK=1`) stands up a REAL
`serve --host 127.0.0.1` + a REAL detached `attention -d --interval 1` and drives
a continuous churn loop for `MDTEST_SOAK_SECONDS` (default 1500s == ~25 min of
active soak, inside a ≤45-min job budget): agent-state records rewritten every
couple seconds across the whole real state vocabulary, plus periodic real
multipart POST /upload + GET /health round-trips. Invariants are sampled
THROUGHOUT and again at the end — heartbeat mtime keeps advancing and never ages
past `log.HEARTBEAT_MAX_AGE`; /health serves and an upload is accepted
byte-identical on disk at minute 25 as at minute 1; both recorded pids survive;
process RSS never runs away (stdlib sampling — `/proc/<pid>/status` on Linux,
`GetProcessMemoryInfo` via ctypes on Windows, `ps` elsewhere — flagged only when
end > 1.8× start AND absolute growth > 64 MiB, a leak guard not a benchmark);
`RotatingFileHandler` keeps ≤ `backupCount+1` bounded files per logger; and the
state store's TTL sweep never destroys the fresh records. It rides a dedicated
nightly workflow (`.github/workflows/soak.yml`, `schedule` + `workflow_dispatch`)
on ubuntu-latest and windows-latest — non-required by design (same posture as
monitor-lab / browser-upload). *Honest gap (small, deliberate):* the upload-accept
path needs a valid multiplexer session, so — like the browser tier's `tmux`-as-`psmux`
symlink — the soak drops a no-op `psmux` shim on the child PATH (exits 0 for
`has-session`/`send-keys`); the file is still genuinely parsed from the multipart
body and written to disk by the product before inject, only the multiplexer
behind the session id is faked.

**Real-multiplexer fleet-control coverage (2026-09-19):** `magent send` / `model`
/ `peek` / `sessions --json` were covered only through `tests/unit/_fake_psmux.py`
— a real on-disk binary that RECORDS argv but is not a terminal — so the argv
magent builds was pinned and the WIRE was not. `tests/e2e/test_fleet_real.py`
(marker `e2e`, rides the existing `end-to-end` job on all three OSes) drives the
real CLI against real detached sessions hosting `tests/e2e/_fleet_agent.py`, a
stand-in that imitates Claude Code's on-screen contract (`❯` input line, `·`
footer, hints row last) and logs every line it reads, so "arrived verbatim" is
read off disk rather than scraped off a screen. *Honest gap (the same one the
browser tier carries):* Linux and macOS have no `psmux` binary, so real `tmux` is
symlinked in as `psmux` on a tmp PATH with a private `TMUX_TMPDIR`; the Windows
leg runs REAL psmux 3.3.8 from the shared `.github/actions/install-psmux`, and a
missing multiplexer FAILS on CI rather than skipping. It paid for itself
immediately, finding three defects a fake terminal structurally could not: send
verification read the pane's LAST line, which on a real Claude Code pane is the
hints row four rows below the input line (so exit 4 was unreachable — the tier
is RED without the fix and reports `OK sent` for a prompt visibly sitting
unsent); `send --compact`'s idle wait believed a reading taken before `/compact`
could take effect, pasting the prompt into a session about to go busy and then
reporting a false exit 4 on it; and `magent peek` died with `UnicodeEncodeError`
whenever stdout was redirected on Windows, because a pane carries the AGENT's
glyphs and a redirected stdout is cp1252.

**The idle reaper is Windows-only (2026-09-27):** off Windows `serve`'s reaper
thread exits at startup (`unsupported platform (no psmux)`), and even with a
multiplexer every session would read `tree-unknown`: POSIX has no Toolhelp
snapshot, no `process_identity`/`terminate_verified`, and no console-membership
check. A port needs all three: a process tree with start times (`/proc` or
`ps`), a kill that pins its target (a pidfd on Linux), and "who shares this
pane's terminal" (controlling-tty/session membership) in place of the console
check. None is built.

**Finished sessions the reaper never parks (2026-09-27):** each errs toward not
parking, and each shows in `reap.log` as a reason that never changes.
Multi-window projects (R3 `shared-cwd`: the state store is keyed by cwd, so a
record cannot say which window it describes; parking one window needs a store
keyed by psmux session); a session nobody has prompted since launch (R8
`no-transcript`); an agent orphaned from its pane tree (R5 `no-agent`); a
finished reply that ends in a question (R9 `pane-dialog`, because
`fleet.classify_state` reads a dialog from phrases such as "do you want"
anywhere on screen; the follow-up, if it fires often, is a narrower R9-only
test, since `send`/`peek` depend on `classify_state` as it is); and placeholder
text in an empty input box, which reads as a `draft`. Codex is out by data: its
`AgentTool` has no `idle_probe`. Per-project account routing moves session
files and transcripts under a per-session `CLAUDE_CONFIG_DIR`, while
`sweep_once` reads one `config_dir`; until the probe takes the per-session map,
a routed pane reads `no-agent`, which is inert and never wrong.

**What a park leaves behind (2026-09-27):** orphaned children of the agent
(already cut from the tree before the snapshot) are not chased, since neither a
parent-pid walk nor `taskkill /T` reaches them; the park line's survivor count
measures what they hold rather than assuming it. Claude Code's own leftovers
(the stale `sessions/<pid>.json` and its `.key`, plugin `.in_use/<pid>` markers,
`session-env/<sid>/`) are deliberately not cleaned: they are undocumented files,
and a wrong delete could hide a live session. A child a process makes during its
own `TerminateProcess` call, after the clock read that bounds its stragglers, is
neither killed nor counted as a survivor: a sub-millisecond window, and never a
wrong kill. The tighter bound, a clock read inside `terminate_verified` after
`TerminateProcess` while the handle still pins the pid, is not built. The psmux server and warm-spare
overhead (about 480 MB of working set per session) is not reclaimed either. And
a parked record can lose its meaning: the store's TTL sweep
(`settings.attention.stateTtlDays`) deletes it, or another writer for the same
directory replaces it. The pane then counts as an ordinary dead pane and a bulk
revive types the configured `--continue` command; the notice in the pane still
names the exact id.

**procs' kernel32 is pinned against a deny list (2026-09-29):**
`test_kill_guards.py` pins five process enders in procs by name
(`TerminateProcess`, `TerminateJobObject`, `NtTerminateProcess`,
`ZwTerminateProcess`, `EndTask`), so a kernel32 call that ends a process by
another name (`_kernel32().GenerateConsoleCtrlEvent`, `DebugActiveProcess`)
passes every procs scan. Only the console helper's kernel32 is a closed list.

**The idle reaper's Windows real-multiplexer legs have not run (POSIX leg
green on a Linux host, 2026-09-28):** `tests/e2e/test_reap_real.py` (a stand-in agent
parked and resumed in a real psmux pane, with draft, dialog, subagent, orphan
and kill-switch variants) is written and rides the `end-to-end` job. Its POSIX
leg ran green against a real tmux on a Linux host, but that leg proves only
that nothing is parked (`tree-unknown`). The Windows park and veto legs, which
carry the proof, have not run anywhere: they were never run on the development
machine, which carries a live fleet, and off CI the tier skips unless
`MDTEST_REAP_REAL=1`. Until a Windows CI run is green, the stop's real-process
proof is the unit tier's win32 tests over processes each test spawns, and the
pane reset, the notice read back through `capture-pane`, and the orphan variant
are proven only against fakes.

**Ten findings carried open into the next audit cycle** (deliberately
triaged out of the fix pass that produced this document, not overlooked):

| Item (provenance) | Substance |
|---|---|
| Phantom `state-sink.mjs` writer (F-NC-001) | **The out-of-repo writer magent's docs name — `state-sink.mjs`, shipped by `ai-agent-notifier` — does not exist.** Driving the real published `ai-agent-notifier@1.0.6` under node (`tests/e2e/test_state_sink_contract.py`) shows its only hook is `src/notify.mjs`, a pure notifier (toast/ntfy/bell) that writes `~/.ai-agent-notifier/.lock-<source>` and **nothing** to `~/.magent/state/`. So a user who wires only `ai-agent-notifier` per README "Where agent states come from" gets an EMPTY state store and a blank `watch`/`attention`. The `node_contract` tier pins this gap and flips RED when any wired hook starts writing magent records. Product decision owed: either ship a real state-writer (in `ai-agent-notifier` or elsewhere) or correct magent's README/`agent_state.py`/`test_agent_state.py`/CLAUDE.md references to `state-sink.mjs`. Codex `notify` (the other named writer) is likewise unverified by any live tier. **RESOLVED (feat/state-hook): magent now ships its own writer** — the `magent-state-hook` console script (`state_hook.py`, stdlib + `agent_state` only), wired into Claude Code's lifecycle hooks by `magent hooks install` (Codex gets a printed `notify` recipe). The phantom `state-sink.mjs` references were corrected to name `state_hook.py`. The npm package remains a pure notifier, and the `node_contract` tier still pins that it writes zero records — that pin now guards against the *notifier* growing a conflicting writer, not against magent lacking one. |
| `IDE_TOOLS` consolidation (F-CT-003) | IDE-vs-CLI-agent tool identity is string-matched in several places instead of one registry — see Key Decisions. |
| Upload server per-request logging (F-IC-001) | `UploadHandler.log_message` routes the stdlib HTTP access log to DEBUG level (deliberately quiet at INFO to avoid logging `?project=` query strings) — so per-request errors surface nowhere at the default level; the rotating `upload` log covers lifecycle events only. |
| Upload retry/robustness (F-IC-003) | The hotkey→server upload path is one HTTP attempt; a flaky mobile/Tailscale link just fails once. |
| Same-second upload filename collision (F-D3-003) | `do_POST` names uploads `f"{int(time.time())}_{basename}"` — two different files for the same project in the same wall-clock second collide. |
| Upload retention sweep (F-D3-004) | `~/.magent/uploads` has no cleanup/retention policy; it grows forever. |
| `init_config.scan_for_projects` scan behavior (F-D5-004) | The "found `.git` dirs, else fall back to flat immediate children" heuristic and the 300-repo cap haven't been re-examined since first written. |
| `init_config` silent `PermissionError` (F-OB-005) | `except PermissionError: continue` skips unreadable directories with no warning that anything was skipped. |
| Hotkey module architecture (F-CT-005) | `hotkey.py` mixes raw ctypes Win32 bindings, hook lifecycle, upload-trigger logic, and pid-file management in one module; a structural split is future work. |
| `agent_state.py` has zero tests (F-IC-007) | No `tests/unit/test_agent_state.py` exists; the module is stdlib-only and eminently testable. |

**Findings recorded during earlier fix passes.** The four `NF-S3` code debts
(001/003/004/005) were burned down in pass-2 (PR-C) and are marked RESOLVED
below; the remainder are still carried in code on purpose (each verified
still true on disk; do not drive-by fix — each needs its own small, tested
change):

- **NF-S3-001 — `_menu_down` echoed success unconditionally. RESOLVED
  (pass-2, PR-C).** `_menu_down` (`cli/status.py`) now branches on
  `stop_server(...)`'s return value exactly like `down_cmd` — success names
  the port, failure prints "not running, or could not be stopped (see
  logs)". Both outcomes covered by
  `test_status.py::TestMenuDownServerReport`.
- **NF-S3-002 — stdout/stderr convention for JSON tests existed nowhere.**
  Click's `CliRunner` merges stdout and stderr into `result.output`; JSON
  assertions that read `result.output` corrupt when any stderr diagnostic
  (e.g. the config version warning) fires. Three sites were fixed during the
  audit; the convention ("JSON-body assertions read `result.stdout`,
  diagnostics via `result.stderr`") is now codified in CLAUDE.md. Pass-2
  (PR-C, P4-01) swept the lone remaining `result.output`-on-a-JSON-body
  instance (`test_status.py:51`) to `result.stdout`.
- **NF-S3-003 — `_generate_docs` (`cli/docs.py`) hand-rolled a drifted schema
  example. RESOLVED (pass-2, PR-C).** The example-config `tools` block is now
  rendered from `Settings().tools` (the `settings_to_dict`/`default_config`
  source), so it lists exactly `DEFAULT_TOOLS` — the fabricated
  `"aider": "aider --model sonnet"` entry is gone — and the `## CLI commands`
  table now lists `magent config migrate`. Pinned by
  `test_cli_smoke.py::test_docs_example_config_tools_match_default_tools`.
  (The still-hand-maintained command table remains a smaller latent-drift
  risk; generating it from the live registration set is future work.)
- **NF-S3-004 — `_attach_nomux` (`cli/attach.py`) hard-coded the fallback
  command. RESOLVED (pass-2, PR-C).** `cmd = _as_str(p.get("cmd")) or
  DEFAULT_TOOLS["claude"]` now derives the fallback from the registry, so it
  can't drift from the default. First-ever coverage of `_attach_nomux` added
  in `test_attach.py::TestAttachNomux`.
- **NF-S3-005 — `status --json` error-shape asymmetry. RESOLVED (pass-2,
  PR-C).** `config_io._load_config_or_exit` gained an `as_json` flag emitting
  `{"ok": false, "error": ...}` on stdout; `status --json` and `up --json`
  both route through it, and `up_cmd`'s inline raw-loader guard was folded
  onto the shared helper (removing the two-path exception). Covered by
  `test_status.py::TestJsonInvalidConfig` and
  `test_attach.py::TestUpJsonConfigError`.
- **`cli/config_editor.py` (637 lines) awaits a further split.** Extracted
  whole from the old monolith; separating the menu-driven `_config_menu`
  from the 14 scriptable `config` subcommands is legitimate next-cycle work,
  gated on `_config_menu`'s characterization pin.
- **No validation on config-editor save.** `config_io._save_raw_config`
  writes whatever raw `dict` it's given; a bad hand-entry made through the
  interactive editor isn't caught until the *next* typed `load_config`
  elsewhere raises `ConfigError` — not at save time. Fix direction:
  validate-after-save (parse the just-written file through `load_config` and
  surface warnings/errors immediately).

**Duplication residue found and left alone (each small, each real):**

- **Tailscale-IP resolution exists in four independent places:**
  `upload_server._tailscale_ip` (used by `_bind_addresses` and, via import,
  by `serve_cmd`'s display), `launch._get_tailscale_ip`,
  `cli/background._tailnet_host` (the most complete: MagicDNS name → Tailscale
  IP → LAN IP), and `cli/mobile.termius_cmd`'s own inline
  `subprocess.run(["tailscale", "ip", "-4"], ...)` block.
- **Two independent pid-liveness checks:** `hotkey._pid_alive` (Windows-only
  ctypes `OpenProcess`/`GetExitCodeProcess`/`STILL_ACTIVE`) and
  `cli/background._pid_alive` (same Windows pattern plus a cross-platform
  `os.kill(pid, 0)` branch) — neither calls the other.
- **`launch.py`'s base-dir expansion chain is duplicated:** the exact
  `os.path.expandvars(os.path.expanduser(base_dir)).replace("/", os.sep)`
  sequence appears in `run_magent`'s body and again in
  `eligible_psmux_projects` — an `_expand_base_dir` helper is the natural
  dedup, not yet extracted.
- **Up/down command/menu twins are close but not shared:** `up_cmd`/`_menu_up`
  and `down_cmd`/`_menu_down` each independently build a
  select-then-act flow around `bring_up_psmux`/`kill_psmux`; the CLI-command
  and menu variants of each have never been unified.

**Tooling and testing gaps:**

- **`tests/` and `scripts/` are now ruff-linted** (resolves the former "tests
  not linted" gap). `scripts/check.py` invokes `ruff check src tests scripts`
  under the expanded ruleset; `[tool.ruff] src = ["src"]` now only declares the
  first-party import root for isort, not the lint scope. Test-specific softening
  lives in `[tool.ruff.lint.per-file-ignores]` `"tests/**"`, one reason per code.
  (Historical note: an audit-era ledger entry called `tests/unit/test_hotkey.py`'s
  `HTTPServer`/`BaseHTTPRequestHandler` imports unused — on the current tree they
  are *used*, by the live-HTTP test harness added later.)
- **The pathlib migration was deliberately trimmed to predicates only
  (LS-A-002 trim).** `os.path.isdir`/`isfile`/`isabs` sites were converted;
  `os.path.expandvars` (no pathlib equivalent) and
  `normpath`/`commonpath`/`relpath` (used in `discover.py`'s merge-key
  normalization and `_find_base_dir`, and `launch.py`'s path resolution)
  were left as `os.path` calls **because converting them can change the
  exact string values other logic keys on**. Converting them for real needs
  semantic-equivalence tests written first, not a mechanical swap.
- **No identity check before force-killing a recorded pid.** Both
  `upload_server.stop_server` and `hotkey.stop_listener` read a pid file and
  kill that pid directly; neither confirms the live process is still the
  *same* process that wrote the file (vs. a recycled pid). A stale file
  after a crash can kill an innocent process.
- **Hook-title read hardening.** The accepted `GetWindowTextW`-in-hook
  design (Key Decisions) names `SendMessageTimeoutW` as the minimal future
  hardening; nobody has done it.
- **`/health` reports service/port/pid/uptime/session-count with no auth** —
  minor information exposure (Low), consistent with the server's
  no-auth-token posture (Key Decisions).
- **`qrcode` has no optional-extras declaration.** It's a graceful
  try/except import with an install tip, so nothing breaks — but
  `pyproject.toml` declares no `[project.optional-dependencies]` extra for
  it. Cosmetic.
- **`cli/attach.py::_tile_titles` continues past "no monitors" and ignores
  the configured grid.** On the attach path, an empty `list_monitors()` logs
  an ERROR and warns the user but the command still exits 0; and it always
  tiles into a hard-coded `compute_grid(monitors, 2, 1)` regardless of the
  config's `layout.columns`/`layout.rows`, unlike the launch path which
  reads the configured grid.
- **The test home isolation leaves `find_config`'s CWD door open.**
  `tests/conftest.py::_isolate_magent_home` moves the HOME family, `APPDATA`
  and `XDG_CONFIG_HOME` into tmp, which closes the last candidate
  `find_config(None)` tries (`env.config_base()/magent/config.json`). The
  first two candidates are relative to the CWD, `./magent.config.json` and
  then `./scripts/magent.config.json`, and the fixture does not move the
  CWD. `magent.config.json` is gitignored precisely because a personal one
  lives at a checkout root, so a test that forgets `--config`, run from
  such a checkout, loads the developer's own config. The only guard is the
  convention that CLI tests pass `--config <tmp_path>`; a global
  `chdir(tmp_path)` would break the tests that rely on a repo-root CWD. The
  cheapest fix is a guard-A-style tripwire that fails any test whose
  `find_config(None)` resolves under the repo root while the redirect is
  active.
- **The home tripwire stops at the HOME family.** Guard B inspects only
  `HOME`/`USERPROFILE` in an explicit child `env=`, so a child env carrying
  the real `APPDATA` or `XDG_CONFIG_HOME` passes it. Guard A's
  `_REAL_STATE_ROOTS` is `~/.magent` and `~/.claude`, without the real
  config base (`REAL_APPDATA/magent` on Windows, `~/.config/magent` or an
  exported `$XDG_CONFIG_HOME/magent` on Linux, `~/Library/Application
  Support/magent` on macOS), so an import-bound Path under any of them is
  not flagged.

## 4. Change guide

Three archetypes cover most future changes.

**(a) Add an agent tool.** For a *plain command tool* (no session resume —
launched as-is, like `cursor-agent`/`agy`), only step 3 applies: one
`DEFAULT_TOOLS` entry plus the example-file update it forces. For a
*deeply-integrated* tool (session resume / multi-window, like
`claude`/`codex`), do all four steps:
1. Add `sessions/<tool>.py` with the same two-function shape as
   `sessions/claude.py`: `get_<tool>_session_ids(project_dir, count,
   config_dir=None) -> list[str | None]` and
   `build_<tool>_resume(base_cmd, session_id) -> str`. `config_dir` is the
   registry's "which of this tool's stores answers for the project"
   argument — claude reads `<config_dir>/projects/<encoded cwd>`, i.e. the
   `CLAUDE_CONFIG_DIR` a routed pane runs under, and `None` is its default
   `~/.claude`. A tool whose store is not account-scoped accepts and ignores
   it (`sessions/codex.py` does; `~/.codex` is one store per machine, and it
   keeps `home_override` as its own keyword-only test seam). Resolve the
   default at CALL time, never as a module-level `Path.home()` constant —
   `tests/conftest.py`'s tripwire exists for exactly that defect class.
2. Add one entry to `AGENT_TOOLS` in `sessions/__init__.py`, wiring those
   two functions in as `session_ids`/`resume_command`; set `happy=True` if
   the tool should be eligible for the Happy mobile/web wrap.
3. If the tool should ship as a built-in default, also add it to
   `config.DEFAULT_TOOLS` — deliberately separate concerns: `DEFAULT_TOOLS`
   controls what config generators pre-populate; `AGENT_TOOLS` controls
   resume/multi-window capability. Changing `DEFAULT_TOOLS` requires
   updating `magent.config.example.json`'s `settings.tools` in the same
   change — `tests/unit/test_config_factory.py::TestExampleConfigMatchesFactory`
   pins the example's settings block to `settings_to_dict(Settings())`
   exactly (that anti-drift pin is the point of the example file).
4. Add a test mirroring `tests/unit/test_tool_registry.py::
   TestOneEditExtensionProof::test_adding_a_tool_is_one_dict_entry` — extend
   `AGENT_TOOLS` via `monkeypatch` and assert the dispatcher picks the new
   tool up with no other code change.

**(b) Add a platform capability:**
1. Add the method (or `supports_*` probe) to the `Platform` ABC in
   `platform/__init__.py` with a safe default — `False` for a probe,
   `raise NotImplementedError(...)` for an operation.
2. Override it per-OS in `platform/windows.py` / `macos.py` / `linux.py`
   only where the backend really has the capability; inheriting the ABC
   default is the correct implementation for backends that don't.
3. Extend `tests/unit/test_platform_contract.py`: parametrize over
   `_DEFAULT_BACKENDS` (`_Bare`, `LinuxPlatform`, `MacOSPlatform`) for the
   default behavior, and add a `@pytest.mark.skipif(sys.platform != "win32",
   ...)` case for the `WindowsPlatform` override (it binds `windll` at
   import, so it can only be exercised on Windows).
4. Gate every call site behind the probe (`get_platform().supports_x()`),
   never a raw `sys.platform` check in business logic.

**(c) Add a CLI command:**
1. New module under `cli/`, importing `main` from `magent.cli.app` (never
   from the package `__init__`) and attaching commands with
   `@main.command(...)`. Follow the import policy: stdlib and leaf imports
   (`config_io`, `ui`, `paths`, `style`, `config` types) at top; heavy
   subsystems (`launch`, `upload_server`, `discover`, `agent_state`,
   `get_platform()`, lazy `hotkey`) in-body with the one-line why-comment.
2. Add the module to the registration import line in `cli/__init__.py` so
   its commands register; add any test-reachable underscore names to that
   file's re-export block/`__all__` only if tests genuinely need them.
3. Expect `tests/unit/test_cli_structure.py`'s `HELP_SNAPSHOTS` matrix to
   change (a new command appears in `--help`); update the snapshots
   deliberately, never by blind regeneration.
4. Add a smoke test invoking the command via the `runner` fixture
   (`runner.invoke(main, [...])`), asserting on `result.exit_code` and a
   stable substring — JSON bodies via `result.stdout` — always against a
   `--config <tmp_path>` config, never real windows/monitors/psmux.

## 5. How this document stays honest

Three mechanisms: **the gate** (`scripts/check.py`: ruff (lint + `format
--check`) + custom lint MD001-MD006 + ty strict + compileall + vulture +
pytest unit tests with a coverage floor, required green before every commit,
so nothing described here as tested or type-checked silently stops being
so); **pins-first discipline** (every relocation described above as "unchanged"
is backed by a characterization test written *before* the change — 
"unchanged" is a checked claim); and the standing rule that **a mismatch
between this document and the code is itself a defect** — fix the document
or flag the code, never silently trust whichever you read first.
