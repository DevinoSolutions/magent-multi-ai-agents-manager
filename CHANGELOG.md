# Changelog

All notable changes to magent are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
