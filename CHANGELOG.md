# Changelog

All notable changes to magent are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[3.1.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/releases/tag/v1.0.0
