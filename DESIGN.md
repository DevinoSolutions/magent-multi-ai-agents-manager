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
  daemons that call it run detached with no console to crash to.
- **`terminals.py`** — `detect_terminal()` + per-OS terminal-priority lists.
  Note for a cold agent: no `src/` module currently calls it —
  `platform/linux.py` and `platform/macos.py` each hard-code their own
  `shutil.which(...)` terminal-priority chain inline instead of calling this
  leaf. It is exercised only by tests. This wasn't raised as a finding in
  the audit that produced this document; flagged here for whoever looks next.
- **`agent_state.py`** — file-per-session lifecycle store (`working`/`done`/
  `needs-input`/`error`/`idle`), keyed by a hash of the session's normalized
  cwd. Stdlib-only by design (its own docstring: it's imported from hook
  handlers on the hot path of every agent turn, so it must stay
  dependency-light). Has zero tests today (see Known Debt).
- **`config.py`** — grouped with subsystems below for its behavioral role,
  but structurally a leaf (no `magent`-internal imports).

### Subsystems

- **`config.py`** — one dataclass schema (`MagentConfig`/`Settings`/
  `ProjectConfig`/`LayoutConfig`/`SSHConfig`), one envelope factory
  (`default_config`), one pair of serializers (`layout_to_dict`/
  `settings_to_dict`) that every config generator delegates through, a pure
  `load_config` reader, and `migrate_config_file` as the single disk-writing
  function in the module. `DEFAULT_TOOLS` is the one dict of built-in
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
  `place_windows` takes one snapshot, places everything already visible, then
  polls only the still-missing set up to the slower of the two deadlines,
  logging a WARNING via `get_logger("launch")` for anything still missing
  before invoking the caller's `on_missing` callback.
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
  dataclass: `session_ids` (a `(project_dir, count) -> list[str|None]`
  callable), `resume_command`, and `happy` (whether the tool can be wrapped
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
- **`hotkey.py`** — the Windows-only Alt+V clipboard-image listener.
  `if sys.platform != "win32": raise ImportError(...)` fires at import time,
  by design — every call site imports it lazily, behind a `supports_hotkey()`
  gate, with a `# ImportError off-Windows (hotkey.py guards); must stay lazy`
  comment at the import. Imports only `log` and `titles` from `magent`.

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
  to `tiling.place_windows` with `settle_s=3` and a hard-coded 2×1 grid —
  see Known Debt), and the `up`/`attach`/`hotkey` commands. Loads typed
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

**`load_config` never writes; `migrate_config_file` is the only writer
(R10).** `load_config` is a pure read: on a schema version below current, it
prints `Warning: config schema v<N> < v<CURRENT>; run: magent config
migrate` to stderr and returns in-memory data — it never touches the file.
Persisting a migration (or backfilled colors) requires `magent config
migrate` (or a save through the config editor's raw path). A load that
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
`magent-name` mode, which still accepts the pre-rename `md-name` alias), so a
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

## 3. Known debt

Ordered roughly by how likely a future change is to collide with it.

**Title badges are ambient state, not guaranteed state (2026-07-07):** the
attention daemon's `BadgeRenderer` rewrites window titles via
`SetWindowTextW`, but shells/terminals with their own title logic (OSC 0/2
sequences, Windows Terminal tab-title settings) can overwrite a badge at any
time. The flash (and toast/ntfy when enabled) are the *reliable* signals;
the badge is best-effort ambience. Revisit only if a real terminal in the
fleet proves badge-hostile in practice.

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
does not implement `launch_psmux_session`, so the test symlinks real `tmux` in
as `psmux` and stands up a real detached `tmux` session on a private socket
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

**Ten findings carried open into the next audit cycle** (deliberately
triaged out of the fix pass that produced this document, not overlooked):

| Item (provenance) | Substance |
|---|---|
| Phantom `state-sink.mjs` writer (F-NC-001) | **The out-of-repo writer magent's docs name — `state-sink.mjs`, shipped by `ai-agent-notifier` — does not exist.** Driving the real published `ai-agent-notifier@1.0.6` under node (`tests/e2e/test_state_sink_contract.py`) shows its only hook is `src/notify.mjs`, a pure notifier (toast/ntfy/bell) that writes `~/.ai-agent-notifier/.lock-<source>` and **nothing** to `~/.magent/state/`. So a user who wires only `ai-agent-notifier` per README "Where agent states come from" gets an EMPTY state store and a blank `watch`/`attention`. The `node_contract` tier pins this gap and flips RED when any wired hook starts writing magent records. Product decision owed: either ship a real state-writer (in `ai-agent-notifier` or elsewhere) or correct magent's README/`agent_state.py`/`test_agent_state.py`/CLAUDE.md references to `state-sink.mjs`. Codex `notify` (the other named writer) is likewise unverified by any live tier. |
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

## 4. Change guide

Three archetypes cover most future changes.

**(a) Add an agent tool.** For a *plain command tool* (no session resume —
launched as-is, like `cursor-agent`/`agy`), only step 3 applies: one
`DEFAULT_TOOLS` entry plus the example-file update it forces. For a
*deeply-integrated* tool (session resume / multi-window, like
`claude`/`codex`), do all four steps:
1. Add `sessions/<tool>.py` with the same two-function shape as
   `sessions/claude.py`: `get_<tool>_session_ids(project_dir, count,
   home_override=None) -> list[str | None]` and
   `build_<tool>_resume(base_cmd, session_id) -> str`.
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
--check`) + custom lint MD001-MD005 + ty strict + compileall + vulture +
pytest unit tests with a coverage floor, required green before every commit,
so nothing described here as tested or type-checked silently stops being
so); **pins-first discipline** (every relocation described above as "unchanged"
is backed by a characterization test written *before* the change — 
"unchanged" is a checked claim); and the standing rule that **a mismatch
between this document and the code is itself a defect** — fix the document
or flag the code, never silently trust whichever you read first.
