<h1 align="center">magent</h1>

<p align="center">
  <strong>Open every project in its own terminal, launch your AI agent, and auto-tile all windows across your screens.</strong><br />
  One command. Every tool. Every monitor.
</p>

<p align="center">
  <a href="https://magent.io"><strong>magent.io</strong></a>
</p>

<!--
  DEMO: docs/media/demo.gif — hero recording goes here.
  Record `magent --go` fanning terminals out across the monitors, auto-tiling them
  into the grid, and an attention badge flipping to [!] when an agent needs input.
  Target ~800px wide, under 10MB. See docs/media/README.md for the shot list.
  Until it's recorded, the ASCII multi-monitor diagram below is the visual stand-in —
  keep it here; do NOT add a broken <img> link before the GIF exists.
-->


<p align="center">
  <a href="https://pypi.org/project/magent-multi-ai-agents-manager"><img src="https://img.shields.io/pypi/v/magent?color=3776AB&label=pypi" alt="PyPI version" /></a>
  <a href="https://pypi.org/project/magent-multi-ai-agents-manager"><img src="https://img.shields.io/pypi/dm/magent?color=blue" alt="PyPI downloads" /></a>
  <a href="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0" /></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+" /></a>
  <img src="https://img.shields.io/badge/dependencies-click-success" alt="Minimal Dependencies" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows" />
  <img src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS" />
  <img src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux" />
</p>

---

```
      Monitor 1  (4K @ 250%)         Monitor 2  (4K @ 250%)        Monitor 3 (1080p @ 175%)
   +------------+------------+    +------------+------------+    +---------+---------+
   |   api      |   web      |    |   infra    |   docs     |    |  ops    |   ...   |
   |  [claude]  |  [claude]  |    |  [codex]   |  [vscode]  |    | [claude]|         |
   +------------+------------+    +------------+------------+    +---------+---------+
                     columns x rows per screen -- true physical pixels on every monitor
```

## Quick Start

> **Available once `v1.0.0` is published to PyPI.** Until then, [install from source](#install-from-source).

```bash
pip install magent-multi-ai-agents-manager          # or: uv tool install magent-multi-ai-agents-manager
magent
```

On first run, magent scans your Claude, Codex, and VS Code history, finds your recent projects, and generates a config. Run it again to launch everything.

## Supported Tools

<table>
  <tr>
    <th>Tool</th>
    <th>Type</th>
    <th>Launch command</th>
    <th>Session resume</th>
    <th>Multi-window</th>
  </tr>
  <tr>
    <td><strong>Claude Code</strong></td>
    <td>CLI agent</td>
    <td><code>claude --continue</code></td>
    <td align="center">Yes</td>
    <td align="center">Yes</td>
  </tr>
  <tr>
    <td><strong>Codex CLI</strong></td>
    <td>CLI agent</td>
    <td><code>codex</code></td>
    <td align="center">Yes</td>
    <td align="center">Yes</td>
  </tr>
  <tr>
    <td><strong>Cursor Agent</strong></td>
    <td>CLI agent</td>
    <td><code>cursor-agent</code></td>
    <td align="center">--</td>
    <td align="center">--</td>
  </tr>
  <tr>
    <td><strong>Antigravity (agy)</strong></td>
    <td>CLI agent</td>
    <td><code>agy</code></td>
    <td align="center">--</td>
    <td align="center">--</td>
  </tr>
  <tr>
    <td><strong>VS Code</strong></td>
    <td>IDE</td>
    <td><code>code</code></td>
    <td align="center">--</td>
    <td align="center">--</td>
  </tr>
  <tr>
    <td><strong>Cursor IDE</strong></td>
    <td>IDE</td>
    <td><code>cursor</code></td>
    <td align="center">--</td>
    <td align="center">--</td>
  </tr>
  <tr>
    <td><strong>Custom</strong></td>
    <td>Any</td>
    <td><em>your command</em></td>
    <td align="center">--</td>
    <td align="center">--</td>
  </tr>
</table>

Add any tool by mapping a name to a shell command in `settings.tools`. CLI agents open in a terminal; IDE tools open via their native CLI (`code`, `cursor`).

### Happy (mobile/web access)

Enable [Happy](https://github.com/slopus/happy) to monitor and control all your AI sessions from your phone or browser with end-to-end encryption:

```json
"settings": { "happy": true }
```

Requires `npm install -g happy`. Supported agents: Claude, Codex. Per-project override with `"happy": true/false`.

### psmux (persistent sessions for SSH access)

Enable [psmux](https://github.com/psmux/psmux) (native Windows terminal multiplexer) so each project runs in a named session you can attach to from anywhere — SSH from your phone, another PC, or a second terminal:

```json
"settings": { "psmux": true }
```

Requires psmux installed (`choco install psmux` or download from GitHub). When enabled, magent creates a detached psmux session per project and opens Windows Terminal attached to it. From any SSH client: `psmux attach -t project-name`.

### Mobile image upload (over Tailscale)

Send screenshots from your phone straight into a project's agent session:

```json
"settings": { "psmux": true, "uploadServer": true, "uploadPort": 8033 }
```

`magent serve` (or `uploadServer: true` during launch) starts a small HTTP server; `magent mobile` prints the phone URL + a QR code you can install as a home-screen app (the QR code needs the optional `qr` extra: `pip install magent-multi-ai-agents-manager[qr]`). Pick a project on the phone, upload an image, and its path is pasted into that project's session. On a desktop browser you can also **Ctrl+V** an image from the clipboard: the page stages it with a preview showing which project it will go to, waits for you to confirm with **Send**, and shows live upload progress until the "pasted into …" confirmation. The Alt+V hotkey (Windows) does the same for whatever `magent:` session is focused.

This works **over Tailscale**: the server binds only the loopback and your machine's Tailscale IP — never the LAN wildcard — and `attach`/`mobile`/`termius` shell out to the `tailscale` CLI to resolve hosts. Devices must be on your tailnet; there is deliberately no auth token, since the bind set is the access control. To bind something else (e.g. LAN-wide), use the escape hatch: `magent serve --host 0.0.0.0`.

## Usage

Run `magent` with no arguments for the interactive menu:

```
           _ _   _    _        _
 _ __ _  _| | |_(_)__| |___ __| |__
| '  \ || | |  _| / _` / -_) _| / /
|_|_|_\_,_|_|\__|_\__,_\___\__|_\_\
  v1.0.0  auto-tile your AI workspace

  ----------------------------------------

   1   Launch & tile new windows  (default)
   2   Re-tile all open windows
   3   Launch a group  AUTOMATIONS | INTERNAL | LEAD-GEN
   e   Edit config
   q   Quit
```

Or skip the menu with flags:

| Command | What it does |
| --- | --- |
| `magent` | Interactive menu. |
| `magent --go` | Launch + tile new windows, no menu. |
| `magent --retile-all` | Re-tile every matching window. |
| `magent -g <name>` | Launch only projects in a group. |
| `magent --init` | Re-scan sessions and regenerate config. |
| `magent --init --base-dir <folder>` | Generate config from a folder of git repos. |
| `magent --edit` | Open config in your default editor. |
| `magent docs` | Print full config reference (Markdown). |
| `magent doctor [--json]` | Diagnose the environment: config, env vars, agent tools on PATH, terminal, monitors, writable dirs, Tailscale, upload port. Exit 1 on any failure. |
| `magent sessions` | List active psmux sessions, pick one to attach. |
| `magent sessions <name>` | Attach directly to a psmux session by name. |
| `magent up [--json] [-g <group>]` | Host side: ensure a persistent psmux session per project. |
| `magent attach <host>` | From another PC: bring host sessions up over SSH, tile locally, Alt+V uploads. |
| `magent watch` | Live table of every agent session, most-urgent first; press a row number to focus that window. |
| `magent attention [-d] [--stop]` | Attention daemon: badges window titles with agent state, flashes the taskbar on needs-input/error, optional toast/ntfy push (`settings.attention`). Badges/flash/toast are Windows-only; ntfy push is cross-platform — see [Platform support](#platform-support). |
| `magent status [--json]` | Session + daemon health (incl. an `agents` state list in `--json`). Exit codes: 0 healthy, 1 config error, 3 degraded. |
| `magent down [--all] [--server]` | Stop sessions; `--all`/`--server` also stop the upload server (and listener). |
| `magent serve [--host <addr>]` | Run the mobile upload server (see below). |
| `magent mobile` | Phone URL + QR code for installing the uploader as a home-screen app. |
| `magent termius` | Generate an SSH config entry that opens the session picker. |
| `magent hotkey` | Run the Alt+V clipboard-upload listener standalone (Windows). |
| `magent config <subcommand>` | Edit config from the CLI — 14 subcommands incl. `migrate`; see `magent config --help`. |

## Platform support

Launching, tiling, and the mobile/notification plumbing run on all three OSes. A few power-user features are Windows-only because they lean on Win32 primitives with no cross-platform equivalent wired up yet. This table is the honest contract — every cell is derived from the capability probes in `src/magent/platform/`, not from aspiration.

| Feature | Windows | macOS | Linux |
| --- | :---: | :---: | :---: |
| Launch + auto-tile across monitors | Yes | Yes | Yes |
| `watch` live fleet table | Yes | Yes | Yes |
| `attention` title badges + taskbar flash | Yes | No | No |
| Desktop toast (`settings.attention.toast`) | Yes | No | No |
| ntfy phone push (`settings.attention.ntfy`) | Yes | Yes | Yes |
| Persistent psmux sessions (`up` / `sessions` / `attach`) | Yes | No | No |
| Global Alt+V clipboard-image hotkey | Yes | No | No |
| Mobile upload server (`serve` / `mobile`) | Yes | Yes | Yes |

Notes:

- **Badges, flash, and toast** are gated on `Platform.supports_attention_signals()`, which returns `True` only in `platform/windows.py`. On macOS/Linux the daemon prints `window badges/flash aren't supported on this OS` and those renderers stay off. Toast additionally uses the Windows-only `winotify` (`[toast]` extra). **ntfy push is cross-platform** — it is stdlib `urllib` over HTTP — so phone notifications work on every OS.
- **The `magent:` title prefix** can be turned off with `settings.windowTitlePrefix: false` — window titles then become the bare project name (e.g. `api` instead of `magent:api`). Launch-path tiling still places windows (it matches the exact title it set), but the features that read the `magent:` grammar degrade to a safe no-op while the prefix is off: the attention daemon's title **badges**, the **Alt+V** clipboard hotkey (which only fires in `magent:`-titled windows), and `magent-name` title matching all stop recognizing your windows. One deliberate exception: `magent attach` windows always keep the prefix — there the title carries the psmux session id that the hotkey chain resolves, so it is load-bearing rather than cosmetic. Leave the setting on unless you specifically want prefix-free titles.
- **Persistent psmux sessions and the Alt+V hotkey** are gated on `supports_psmux()` / `supports_hotkey()` (also Windows-only). Off Windows the psmux entry points raise `NotImplementedError` and importing `hotkey` raises `ImportError`.
- The **mobile upload server** itself (serving the PWA over loopback + Tailscale and receiving images) runs everywhere; auto-pasting the uploaded path into a *live* agent session uses psmux, so that last hop is Windows-only. Likewise, `watch`'s table renders on every OS but its press-a-number-to-focus action uses the same Windows-only window primitives.

## Where agent states come from

`magent watch`, `magent attention`, and `magent status --json` do not poll your agents directly. They read per-session **state records** — `working`, `needs-input`, `done`, `error`, `idle` — that your coding agent writes through its lifecycle hooks: Claude Code hooks and Codex's `notify` hook. Until those hooks are wired, the state store stays empty and `magent watch` shows an empty table.

The companion [`ai-agent-notifier`](https://www.npmjs.com/package/ai-agent-notifier) package (same authors) installs those hooks for you across Claude Code, Codex, and Cursor:

```bash
npx ai-agent-notifier setup
```

The setup wizard detects your installed agents and wires the hooks; restart your agents to activate. After that, `magent watch` and `magent attention -d` light up as your agents change state.

## Configuration

Config is stored at a platform-standard location:

- **Windows:** `%APPDATA%\magent\config.json`
- **macOS:** `~/Library/Application Support/magent/config.json`
- **Linux:** `~/.config/magent/config.json`

Or place `magent.config.json` in your working directory (it is gitignored — your personal config never gets committed).

Start from the committed sample, [`magent.config.example.json`](magent.config.example.json) — it is generated from the config factory and exercises every surface (groups, remote `host`/`remotePath`, `ssh`, the full `settings` block):

```json
{
  "version": 1,
  "baseDir": "C:/Users/you/projects",
  "layout": { "columns": 2, "rows": 1 },
  "settings": { "defaultTool": "claude", "...": "see the example file / magent docs" },
  "projects": [
    { "path": "backend/api", "group": "backend", "tool": "claude", "color": "#3b82f6" },
    { "path": "gpu-worker", "group": "infra", "host": "gpu-box.example.com", "remotePath": "/home/dev/worker", "tool": "codex" }
  ]
}
```

Configs are versioned (`"version": 1`). A config without a current version still loads but prints a warning until you run `magent config migrate` — loading never rewrites your file; `migrate` is the only writer (it also persists auto-assigned project colors; those are derived deterministically from each project's title/path, so they stay the same every run even before you migrate).

### Project fields

| Field | Default | Description |
| --- | --- | --- |
| `path` | *(required)* | Absolute, or relative to `baseDir`. |
| `group` | none | Tag for group launches (`-g`). |
| `tool` | `defaultTool` | `claude`, `codex`, `cursor-agent`, `agy`, `vscode`, `cursor`, or any custom tool. |
| `color` | derived | Terminal tab color (`#rrggbb`); auto-derived from the project title/path when unset. |
| `title` | folder name | Window title for matching. |
| `enabled` | `true` | Set `false` to skip without deleting. |
| `happy` | inherit | Override global Happy setting for this project. |
| `host` | none | SSH target for remote projects. |
| `remotePath` | `path` | Remote directory when different from `path`. |
| `windows` | none | List of window objects `{"name", "tool", "command"}` with per-window tool/command overrides. Legacy `int` / `["name1", "name2"]` forms still parse. |

### Multi-window sessions

Open the same project in multiple windows. `windows` is a list of window objects, each with optional per-window `tool`/`command` overrides:

```json
{
  "path": "api",
  "windows": [
    { "name": "api" },
    { "name": "api-2" },
    { "name": "api-codex", "tool": "codex" }
  ]
}
```

`name` sets the window title; `tool`/`command` override the project's defaults for that window only. Windows without an override each resume the Nth most recent Claude/Codex session.

The legacy `"windows": 3` and `"windows": ["api", "api-2"]` forms still parse and are normalized to window objects by `magent config migrate`.

### Remote projects

```json
{ "host": "deploy@server", "path": "/srv/api", "tool": "claude" }
```

CLI agents run over SSH. VS Code/Cursor projects open via Remote-SSH.

### Custom tools

```json
"tools": {
  "claude": "claude --continue",
  "codex": "codex",
  "cursor-agent": "cursor-agent",
  "agy": "agy",
  "aider": "aider --model sonnet",
  "shell": "bash"
}
```

## Testing

<table>
  <thead>
    <tr>
      <th>Job</th>
      <th align="center" width="180">Live status</th>
      <th>Platforms</th>
      <th>What it verifies</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>Unit</strong></td>
      <td align="center"><a href="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml"><img src="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI" /></a></td>
      <td>Windows / macOS / Linux<br/>Python 3.10 -- 3.14</td>
      <td>Config parsing, grid computation, title generation, session resume, discovery, grouping (15 matrix jobs)</td>
    </tr>
    <tr>
      <td><strong>Platform</strong></td>
      <td align="center"><a href="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml"><img src="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI" /></a></td>
      <td>Windows / macOS / Linux</td>
      <td>Real monitor detection (ctypes/Swift/xrandr), real window find+move, real terminal launch, DPI scaling</td>
    </tr>
    <tr>
      <td><strong>E2E</strong></td>
      <td align="center"><a href="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml"><img src="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI" /></a></td>
      <td>Windows / macOS / Linux</td>
      <td>Full CLI dry-run, config loading, group filtering, SSH project handling, vscode/cursor tool alias, multi-window</td>
    </tr>
    <tr>
      <td><strong>Packaging</strong></td>
      <td align="center"><a href="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml"><img src="https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI" /></a></td>
      <td>Windows / macOS / Linux</td>
      <td>Build wheel, install into a pristine no-extras venv, drive the real installed <code>magent</code> entry point: version/help, dev-dep import sweep, virgin first-run, socket-real serve, optional-extra degradation, and a real window spawn (win32)</td>
    </tr>
  </tbody>
</table>

### Run it yourself

```bash
pip install -e ".[dev]"
pytest tests/unit/ -q                        # fast, safe anywhere
pytest tests/e2e/ -m "e2e and not needs_ssh" # subprocess dry-runs; no SSH server needed
pytest tests/platform/ -v -m platform        # real monitors/terminals — CI-grade env only
pip install build && pytest tests/dist/ -m dist  # wheel -> pristine venv -> real installed entry point
python scripts/check.py                      # the quality gate: ruff + custom lint + ty + compileall + vulture + pytest w/ coverage
```

A bare `pytest` collects **all** tiers, including tests that enumerate real monitors, launch real terminals, and expect an SSH server — run those only in an environment set up like CI (`.github/workflows/ci.yml`). `scripts/check.py` is the repo's commit gate; it must pass before every commit.

## Cross-platform support

| Feature | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Monitor detection | ctypes Win32 | Swift/AppKit | xrandr |
| Window management | EnumWindows/MoveWindow | AppleScript | xdotool/wmctrl |
| Terminal | Windows Terminal | kitty/iTerm/Terminal.app | kitty/alacritty/gnome-terminal |
| DPI awareness | Per-Monitor V2 | Native Retina | xrandr DPI |

## Install from source

```bash
git clone https://github.com/DevinoSolutions/magent-multi-ai-agents-manager.git
cd magent-multi-ai-agents-manager
pip install -e .
```

## Contributing

Contributions are welcome. Please open an issue first to discuss what you'd like to change.

## License

[AGPL-3.0](LICENSE) -- Copyright (c) 2026 [DevinoSolutions](https://github.com/DevinoSolutions)
