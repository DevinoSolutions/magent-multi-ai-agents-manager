# Changelog

All notable changes to magent are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.10.6] - 2026-08-03

### Fixed

- **Agent commands can no longer be silently swallowed at bring-up.** A
  fresh psmux session is a bare shell and the agent command is TYPED into
  it via send-keys -- a channel with no delivery acknowledgment. Under a
  spawn storm a still-initializing PowerShell can flush pending console
  input and swallow the command outright: the pane then rests at an empty
  prompt forever while passing every liveness probe, and the revive net
  never sees it (it only sweeps sessions that existed before the
  bring-up). Bring-up now verifies delivery: after each batch's sends,
  every pane's current process is probed in one fan-out and any pane
  still at the bare shell gets the configured command re-typed --
  bounded at two retries, never injecting into a pane whose agent is
  already running (or whose state can't be read), with a warning per
  re-send and an error when a pane stays bare. Costs one 2s settle per
  batch of five on a healthy bring-up.

## [3.10.5] - 2026-08-02

### Changed

- **The status bar only advertises F2 where VS Code exists.** F1 (detach
  back to the picker) is a psmux binding magent installs on the session's
  own server, so it works for any viewer; F2 is handled by the magent
  hotkey listener, which shells out to VS Code's `code` launcher -- on a
  machine with no VS Code the hint advertised a key that would do nothing.
  The `F2 </> VS Code` half is now emitted only when `code` resolves on
  the machine decorating the session (probed once per batch); without it
  the bar reads `F1 Proj. Picker` alone. Both variants stay pure ASCII --
  the 3.10.4 invariant now pins each half on its own. tmux status lines
  are session-scoped, not per-client, so the hint reflects the session
  host's `code`; a per-viewer hint is outside the protocol.

## [3.10.4] - 2026-08-02

### Fixed

- **The status-bar hint no longer corrupts the bar.** The ☰ menu glyph
  shipped in 3.10.3 is U+2630, an East-Asian *ambiguous*-width codepoint:
  psmux counts it as one cell, Windows Terminal draws it as two, so every
  cell after it shifted right -- a stray highlighted cell inside the bar
  and, when the shift spilled the last column, a wrapped phantom row of
  black space under it. The hint is now pure ASCII
  (`F1 Proj. Picker   F2 </> VS Code`), pinned by a test that fails before
  any non-ASCII glyph can go back in.

## [3.10.3] - 2026-08-02

### Fixed

- **Frozen attach windows can no longer masquerade as "ready".** After a
  laptop sleep or a network change, the TCP connection under an attach
  window dies but the ssh client keeps running -- OpenSSH sends nothing on
  an idle session, so the zombie blocks forever. The dead-window scan
  judges liveness by "a live ssh/psmux process carries this session's
  attach command", and a zombie carries it -- so the window counted as
  live and "already open", the overview said `N/N ready` with no
  annotation, and no sweep would ever repair the frozen pane. Attach
  windows now spawn ssh with keepalives (`ServerAliveInterval=15`,
  `ServerAliveCountMax=4`): a dead connection makes the client exit at
  most ~60s later, the pane becomes an ordinary `[process exited]` corpse,
  and the existing sweep closes and reopens it. The host-side psmux
  session is untouched by the client dying -- no work is lost.
- **Sessions that can never come up now say why.** A project whose folder
  does not resolve on the host (or has no agent command, or no psmux
  binary) was reported down forever with zero explanation -- bring-up
  silently skipped it every time. The down entry now carries a `reason`
  and the session overview renders it next to the name, e.g.
  `eBay (folder not found)`. Probed-but-down sessions stay reason-less:
  ordinary down needs no explanation.

### Changed

- **The psmux status-bar hints are readable.** `F1 picker  F2 code` was
  four bare tokens with nothing marking which half is a key. Each key name
  is now a bold accent badge and each label says what the key does:
  `F1 ☰Proj. Picker   F2 </> VS Code`. No special fonts required, and the
  status bar sets its own width budget so a personal tmux config can no
  longer truncate the hint mid-label.

## [3.10.2] - 2026-08-02

### Fixed

- **`magent attach` now clears terminated terminals whose session is gone
  too.** The corpse repair shipped in 3.10.0/3.10.1 only examined windows
  whose session was UP on the host, so a dead pane left by a previous
  session -- host rebooted, session killed earlier, or a declined bring-up
  -- was never scanned and sat in the grid through every subsequent
  attach. The start-of-attach pass now sweeps EVERY local `magent:`
  window: a dead pane whose session is up is closed and reopened (as
  before); a dead pane whose session is not up is closed for good, and
  named in the pre-prompt annotation as a dead window from a previous
  session. A live window outside the current session list -- for example
  another group's windows under `-g` -- is left strictly alone: liveness,
  never list membership, decides.
- **Locally-launched psmux windows can no longer be mistaken for
  corpses.** The liveness marker matched the literal `psmux -L <session>
  attach`, but locally-launched windows exec the resolved absolute path to
  psmux.exe. Harmless while only remote up-sessions were examined; fixed
  now that the sweep judges every `magent:` window.

## [3.10.1] - 2026-08-02

### Fixed

- **A big `magent attach` no longer ends with dead panes from the SSH
  spawn storm.** Each window opens its own SSH connection (Windows OpenSSH
  has no connection sharing), and while the host is cold-starting dozens of
  agents its handshakes stretch past sshd's concurrent-handshake limit
  (`MaxStartups`), which then drops newcomers -- panes stuck on
  `Connection closed by <host> port 22` or `kex_exchange_identification:
  read: Connection reset`. Attach now verifies its own spawns after tiling
  settles: panes that died at the handshake are closed and respawned with a
  slower stagger, re-tiled, and checked once more. Anything still dead
  after two attempts is reported (its pane keeps the SSH error on screen)
  with a hint to re-run attach -- never a silent corpse, never an infinite
  loop.
- **The session overview no longer calls a session "ready" while its
  window here is dead.** The overview is host truth (the psmux session
  really is up); when the local pane is a corpse, attach now says so right
  under the table -- naming the sessions and noting they will be closed
  and reopened -- before the bring-up prompt, while you are still
  choosing.

## [3.10.0] - 2026-08-02

### Added

- **`magent down` acts on the attach host.** On an attach client there are
  no local psmux sessions, so `magent down --all` used to stop the local
  Alt+V listener and nothing else -- while attach's own goodbye line
  advertises that exact command. Now, when nothing matches locally and the
  machine has attached to a host before, the shutdown is forwarded over SSH
  to that host (a new `--host <target>` option overrides the choice
  explicitly), forwarding names / `-g` / `--all` / `--server` verbatim and
  carrying the remote exit code. Local attach windows are closed first --
  killing the remote sessions would otherwise strand one dead pane per
  window.

### Fixed

- **`magent attach` repairs dead attach windows instead of skipping them
  forever.** When an attach window's SSH connection dies, Windows Terminal
  keeps the pane open (`[process exited with code 255]`) with its
  `magent:<session>` title intact -- and attach's title-based dedupe then
  counted that corpse as open and never reopened the session. Attach now
  verifies each open window has a live attach client (an `ssh`/`psmux`
  process running that session's attach command); a dead pane is closed
  gracefully and the session's window respawned. Deliberately conservative:
  if the process scan itself fails, no window is touched.

## [3.9.1] - 2026-07-31

### Fixed

- **"Re-tile all open windows" no longer duplicates open windows or
  reopens closed ones.** Two defects, one report. First, for psmux
  projects the launch pipeline collected every window for create+attach
  before checking whether that window was already open -- and each attach
  spawns a brand-new terminal window with no dedupe of its own (psmux's
  `has-session` probe only dedupes sessions), so any re-run with windows
  open produced one duplicate per open session. The collection is now
  gated on the same window-open probe the plain-terminal path always
  used: an open window is left untouched, a closed window with a live
  session gets reattached, and a dead session is recreated. Second, menu
  option 2 and a bare `--retile-all` rode the full launch pipeline, so a
  window you deliberately closed came back. Both are now tile-only: they
  place what is open and launch nothing. `--go --retile-all` keeps the
  combined meaning -- launch whatever is missing, then tile everything.

## [3.9.0] - 2026-07-31

### Added

- **`magent status` shows the psmux sessions your agents live in -- and
  acts on them.** The status report now lists each live session with its
  foreground app (or `idle` when the agent fell back to a shell) and its
  agent state, read from the same store the session picker uses. From the
  interactive menu you can attach this terminal to any listed session or
  revive a stopped agent in place. `status --json` gains an additive
  `psmux_sessions` key; the envelope shape and 0/1/3 exit codes are
  unchanged. Probes fan out concurrently, so a large session table still
  costs about one psmux round-trip.
- **magent brands its own status bar.** Every magent psmux session now
  sets its status-left to ` magent ` (with the width budget it needs), so
  the branding is consistent per session and wins over whatever a personal
  `~/.tmux.conf` set. `magent up` refreshes it on already-running
  sessions, same as the F1/F2 hints.

### Changed

- **Pre-rename "md" leftovers renamed to magent.** The mobile upload page
  is now "magent upload" end to end -- page title, logotype, iOS install
  banner and Web Clip profile, the downloaded `.mobileconfig` filename,
  and the PWA's cache/storage keys (the service worker cleans up the old
  cache on upgrade; the install banner may reappear once on existing
  mobile clients). Internal `md_*` helper names followed. Test-tier
  gates (`MDTEST_*`), the CI `mdssh` alias, and the `MD001`-`MD005` lint
  rule IDs are deliberately unchanged.

## [3.8.0] - 2026-07-30

### Added

- **Focusing a window reclaims its geometry, continuously.** The hotkey
  listener now watches window focus: whenever a magent window gains focus
  it re-nudges it (debounced per project, never while a mouse button is
  down), so the window you're looking at always carries your machine's
  geometry even if another attached client resized behind your back.
  Extends v3.6.0's attach-time reclaim into standing protection; reaches
  running listeners automatically via the listener auto-restart.

## [3.7.0] - 2026-07-30

### Added

- **Edit a remote host's config over SSH.** `magent config edit` (defaults
  to the last attach host; or `magent config edit user@host`) fetches the
  host's config, opens it in your JSON editor, validates it on close, and
  pushes it back only if valid and changed. The host validates again,
  backs up the previous config, and writes atomically -- an editing
  mistake can never corrupt the remote config. Powered by two new
  host-side subcommands, `config cat` and `config put`; hosts too old to
  have them get a clear upgrade hint.

## [3.6.0] - 2026-07-29

### Added

- **Attach reclaims the session geometry for your machine.** psmux sizes a
  session to whatever client sent the last resize event and never
  recomputes (its `window-size` options are stored but unimplemented), so
  sessions viewed from another machine rendered clipped/squeezed until a
  manual zoom nudged the terminal. `magent attach` now nudges every window
  it handles past a cell boundary and restores its exact tiled rect,
  forcing the local client's size to win -- the manual Ctrl+/- trick,
  automated.

### Fixed

- **A stale hotkey listener is restarted instead of trusted.** The Alt+V /
  F2 listener now records its version, server URL and SSH host; attach and
  local launches restart it whenever any of those don't match (e.g. after
  a pip upgrade, or switching between local and remote use). Previously
  the single-instance guard kept the old process alive with the old code,
  leaving F2 silently dead after an upgrade.

## [3.5.0] - 2026-07-29

### Added

- **F2 reports its progress in the window's own status line.** Pressing F2
  now flashes what is happening -- "F2: opening VS Code...", "F2: 'code' not
  found on PATH", "F2: no folder known for <project>", "F2: VS Code ->
  <folder>" -- instead of failing silently into `hotkey.log`. The feedback
  rides the same status-line flash channel upload progress already uses, so
  it works identically over a remote attach.
- **Local launches start the hotkey listener too.** `magent --go` and the
  interactive menu now start the Alt+V / F2 listener (single-instance
  guarded, pointed at loopback), so the `F2 code` status-bar hint is live
  everywhere -- previously only `magent attach` started it.
- **Attach warns when the host runs an older magent.** `magent up --json`
  now reports the host's version and the attach client prints a loud
  warning naming both versions and the upgrade command when they differ --
  missing features now point at the real cause instead of failing silently.

### Fixed

- **Remotely-attached sessions get the F1/F2 status-line hints.** The
  `up --json` path that `magent attach` drives on the host never decorated
  pre-existing sessions; only fresh bring-ups got the hints.

## [3.4.0] - 2026-07-29

### Added

- **Window hotkeys are advertised, and F2 opens the project in VS Code.**
  Every magent session now shows `F1 picker  F2 code` in its psmux status
  line. F1 keeps its meaning (detach, back to the picker) and is now set by
  magent itself, so it works on machines without a personal tmux binding.
  Pressing F2 in a magent window opens that project's folder in VS Code --
  over Remote-SSH when you're attached to another machine, so the editor
  runs where you're sitting and the files stay where they live.

### Fixed

- **Re-attaching no longer duplicates windows.** `magent attach` now checks
  which projects already have an open window and only spawns the missing
  ones; existing windows are simply tiled into the grid with everything
  else.

## [3.3.0] - 2026-07-29

### Added

- **`magent up` and `magent attach` revive dead agents.** A session whose
  agent was Ctrl-C'ed (or whose launch keystrokes never landed) still counts
  as "up", so it used to come back as a window parked at a bare shell.
  Both commands now spot a pane resting at a shell prompt and re-type the
  agent's command into it -- for Claude that is `claude --continue`, so the
  conversation picks up right where it was killed. `up --json` stays a pure
  read unless the new `--revive` flag is passed, and attaching to a host on
  an older magent falls back cleanly.

### Fixed

- **Duplicate config entries no longer spawn twin windows.** Two entries
  pointing at the same project produced two identically-titled windows, the
  second of which could never be tiled. One session id now maps to one
  session, first entry wins.
- **Session statuses are read from the right pane.** The picker's
  working-directory probe answered for the *caller's* pane when magent was
  itself run from inside a psmux session, which could mislabel every
  session's status column.

### Changed

- **`magent sessions` paints in about a third of the time.** With 40 live
  sessions the first paint dropped from ~4.8s to a projected ~1.5s: the
  per-session working-directory probes are gone (config already knows each
  session's directory, since magent created it there), and the liveness
  sweep now launches every probe at once instead of sixteen at a time.

## [3.2.2] - 2026-07-28

### Changed

- **The attach flow sheds its remaining serial waits.** Windows spawn on a
  0.25-second stagger instead of 0.4 (the real limit is the host's SSH
  connection throttle, and this stays well under it); the upload-server
  check rides under the tiling poll instead of adding its own round-trip at
  the end; the post-bring-up poll stops the moment every expected session is
  up instead of paying ~10 seconds to prove a stable count it already has;
  and each bring-up wave on the host waits on its shells under one shared
  10-second deadline instead of 10 seconds per session. A 40-window warm
  attach lands around 12-14 seconds end to end.

## [3.2.1] - 2026-07-28

### Fixed

- **Attached windows align the moment they appear.** Tiling used to sleep a
  fixed budget scaled to the window count (40 windows meant a hard 40-second
  wait) before taking its first look at the screen -- so an attach whose
  sessions already existed sat on fully-open, untiled windows for the whole
  budget. That budget is now a deadline for latecomers only: placement polls
  from the first second, moves each window as soon as it shows up, and
  finishes immediately once everything is placed.

### Changed

- **The CLI boots faster.** The package version is now resolved lazily, so
  every command -- including `--help` and the interactive menu -- skips the
  package-metadata machinery (roughly 75ms of a ~450ms boot) unless it
  actually prints a version.

## [3.2.0] - 2026-07-28

### Added

- **`magent attach` remembers your last host.** A no-argument attach now
  defaults its host prompt to the last target that successfully answered a
  status query (persisted at `~/.magent/last-attach-host`) -- press Enter to
  reuse it. The config-derived guess remains the fallback, and a host is only
  remembered after it answers, so typos and dead hosts never become the
  default.

## [3.1.5] - 2026-07-25

### Fixed

- **Big attaches stay reliable under load.** Sessions now come up in waves of
  five (instead of cold-starting every agent at once, which starved the host),
  and each pane's shell gets a beat to be ready before the agent command is
  typed, so no session is left sitting at a bare prompt.
- **The session picker survives an overloaded host.** The liveness scan is
  concurrent with a retry (it was 40 serial probes that silently dropped live
  sessions), a direct `magent sessions <name>` resolves from config without
  the scan, and a failed attach is reported and retried instead of silently
  bouncing back to the menu.
- **Remote attach windows connect in about a second.** Each window now runs a
  direct psmux attach (with the picker as fallback) instead of booting the
  whole magent CLI first -- a 40-window attach was taking minutes on that
  alone. Tiling also honors the attaching machine's configured grid (it was
  hardcoded 2x1) and scales its settle budget with window count, so
  late-spawning windows get placed instead of being left behind.

## [3.1.4] - 2026-07-25

### Fixed

- **`magent attach` no longer gives up on a long bring-up.** Cold-starting
  many agents at once is a several-minute storm on the host; attach previously
  abandoned it after 300 seconds and opened windows from one snapshot of a
  still-growing session list, so most windows missed their session. The
  bring-up budget is now 15 minutes and the requery polls until the host's
  session count stops growing.

## [3.1.3] - 2026-07-25

### Fixed

- **Sessions brought up over SSH started without their agent.** On the host
  side of `magent attach`, sshd kills the remote command's process tree the
  moment `magent up` returns -- which silently killed the fire-and-forget
  `send-keys` processes that type each agent command into its new psmux
  session. Every session came up as a bare shell, so the attached windows
  showed an empty prompt (or fell back to the session picker) instead of the
  agent. The keystroke senders are now awaited, guaranteeing the agent command
  lands before the CLI exits.

## [3.1.2] - 2026-07-25

### Fixed

- **`magent hooks install` wired hook commands that never ran on Windows.**
  Claude Code executes hook commands through a POSIX shell, which consumed the
  backslashes in the installed path -- so the state hook silently failed on
  every lifecycle event and session states stayed stale despite a "wired"
  `hooks status`. Paths are now written in forward-slash form, and re-running
  `magent hooks install` after upgrading repairs entries wired by 3.1.0/3.1.1
  in place (foreign hooks untouched).
- **`magent attach` now says why the project-status read failed.** SSH
  timeouts, ssh/auth errors, and magent-missing-on-host previously collapsed
  into one generic "Could not read project status" line. The status query also
  retries once with a 120-second timeout when the host is slow to answer --
  a just-booted machine cold-starts far slower than the old hard 30-second
  budget allowed.

## [3.1.1] - 2026-07-25

### Fixed

- The interactive-menu ASCII banner (and the README demo) still rendered the
  project's pre-rename wordmark; it now spells **magent**.

## [3.1.0] - 2026-07-24

### Added

- **`magent hooks install`** wires the new bundled `magent-state-hook` writer
  into Claude Code's lifecycle hooks (idempotent; existing hooks are preserved)
  and prints the Codex `notify` recipe — closing the gap where no writer ever
  fed the session-state store, so the session picker, `magent watch`, and the
  attention daemon showed missing or stale `done` / `needs input` states.
  `magent hooks status` shows what is wired and how fresh the store is.
- The session picker now shows a live **`still going... <minutes>`** label
  while a turn is in flight; the hook refreshes the record on every tool call,
  so the label tracks real activity rather than just the turn's start.

## [3.0.0] - 2026-07-24

### Fixed

- `--version`, the menu banner, and `magent docs` reported a stale hardcoded
  version; the version now derives from package metadata.

### Removed

- **Pre-rename config-directory fallback dropped.** magent now reads config only
  from `~/.magent`; the read-only fallback to the old pre-rename config directory
  is gone. This is **breaking** for any install still pointing at that old
  location: move the config into `~/.magent` (or pass `--config`) before upgrading.
- **Deprecated tiling placement alias dropped.** `magent-name` is now the only
  accepted placement mode; the old pre-rename spelling is no longer recognized.
  This is **breaking** for any config or producer still using it — switch it to
  `magent-name`.
- **`sentry` compat-alias extra dropped.** `sentry-sdk` is a base dependency, so
  `pip install "magent-multi-ai-agents-manager[sentry]"` no longer resolves.
  Install the package normally; error reporting stays env-gated via
  `MAGENT_SENTRY_DSN`.

## [2.0.0] - 2026-07-24

### Added

- **`settings.windowTitlePrefix`** (default `true`) — set `false` to drop the
  `magent:` prefix and use bare project-name window titles. Launch-path tiling
  falls back to exact-title matching so windows still place; the attention
  daemon's title badges, the Alt+V hotkey, and `magent-name` title matching
  depend on the prefix and quietly no-op while it is off.

### Changed

- **Project renamed from multideck to magent.** The PyPI distribution is now
  `magent-multi-ai-agents-manager` (`pip install magent-multi-ai-agents-manager`),
  the CLI command and import package are both `magent`, and the project homepage
  is <https://magent.io>. The GitHub repository moved to
  `DevinoSolutions/magent-multi-ai-agents-manager`.
- **Environment variable prefix is now `MAGENT_*`** (for example, the Sentry DSN
  is read from `MAGENT_SENTRY_DSN`).
- **User config/data directory is now `~/.magent`** (config, logs, agent state,
  `.env` file, lockfile).
- **Window-title prefix is now `magent:`** in the title grammar.
- **Tiling placement mode is now `magent-name`.**

## [1.0.0] - 2026-07-12

Initial public release. (1.0.0 shipped under the project's original name, now
deprecated.) magent opens every project in its own terminal, launches its AI
agent, and auto-tiles every window across every monitor — one command, every
tool, every screen.

### Added

- **Launch & tile pipeline** — `magent` (interactive menu), `magent --go`
  (skip the menu), `magent -g <group>` (launch one group), and
  `magent --retile-all` open each configured project in its own terminal, start
  its agent, and tile every window into a per-screen columns×rows grid with true
  physical-pixel placement and per-monitor DPI awareness on Windows, macOS, and
  Linux.
- **Zero-config bootstrap** — first run scans your Claude, Codex, and VS Code
  history to generate a starter config; `magent --init [--base-dir <folder>]`
  regenerates it from recent sessions or a folder of git repositories.
- **Session resume for Claude Code and Codex** — deeply integrated CLI agents
  resume the most recent session per window (`claude --continue`, `codex`), and each
  additional window resumes the next-most-recent session. Cursor Agent, VS Code,
  Cursor, and arbitrary custom tools are also supported.
- **Per-window tool & command overrides** — a project's `windows` list opens the
  same project in several windows, each with optional per-window `tool`/`command`
  overrides; the legacy `int` and `["name", …]` window forms still parse and are
  normalized on migration.
- **psmux persistent sessions + SSH attach (Windows)** — with `settings.psmux`,
  each project runs in a named, detached psmux session you can reattach from
  anywhere. `magent up` ensures a session per project, `magent sessions`
  lists and attaches them, `magent attach <host>` brings a host's sessions up
  over SSH and tiles them locally, and `magent termius` emits an SSH config
  entry that opens the session picker.
- **Attention stack** — `magent attention -d` runs a daemon that badges window
  titles with each agent's state, flashes the taskbar on needs-input/error, and can
  push toast/ntfy notifications (`settings.attention`); `magent watch` shows a
  live, most-urgent-first table where a digit key focuses that window. Agent state
  is fed by Claude Code hooks and Codex notify.
- **Mobile image upload over Tailscale** — `magent serve` runs a small HTTP
  upload server bound only to loopback and your Tailscale IP (never the LAN
  wildcard; `--host` is the escape hatch), `magent mobile` prints a phone URL and
  QR code for a home-screen web app, and the Alt+V hotkey (Windows) uploads the
  clipboard image into the focused session. There is deliberately no auth token —
  the bind set is the access control.
- **Diagnostics, status & lifecycle** — `magent doctor [--json]` diagnoses the
  environment (config, env vars, agent tools on PATH, terminal, monitors, writable
  dirs, Tailscale, upload port), `magent status [--json]` reports session and
  daemon health with actionable exit codes (0 healthy, 1 config error, 3 degraded),
  and `magent down [--all] [--server]` stops sessions and, optionally, the upload
  server.
- **Config schema v3 + migration** — typed, versioned JSON config with an
  interactive editor, `magent config` (14 subcommands, including `migrate`), and
  `magent docs` (generated schema reference). `magent config migrate`
  normalizes legacy window forms, stamps the current schema version, and persists
  deterministically derived project colors; loading a config never rewrites it.
- **Packaging** — installs as the `magent` console script with a minimal core
  (`click`, `pydantic-settings`) and optional extras for Windows toast
  notifications (`toast`) and QR rendering (`qr`). Sentry error reporting is
  env-gated via `MAGENT_SENTRY_DSN`.

[3.10.6]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.5...v3.10.6
[3.10.5]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.4...v3.10.5
[3.10.4]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.3...v3.10.4
[3.10.3]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.2...v3.10.3
[3.10.2]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.1...v3.10.2
[3.10.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.0...v3.10.1
[3.10.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.9.1...v3.10.0
[3.9.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.9.0...v3.9.1
[3.9.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.8.0...v3.9.0
[3.8.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.7.0...v3.8.0
[3.7.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.2.2...v3.3.0
[3.2.2]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.2.1...v3.2.2
[3.2.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.2.0...v3.2.1
[3.2.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.1.5...v3.2.0
[3.1.5]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.1.4...v3.1.5
[3.1.4]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.1.3...v3.1.4
[3.1.3]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.1.2...v3.1.3
[3.1.2]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.1.1...v3.1.2
[3.1.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/releases/tag/v1.0.0
