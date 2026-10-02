<h1 align="center">magent</h1>

<p align="center">
  <strong>Open every project in its own terminal, launch your AI agent, and auto-tile all windows across your screens.</strong><br />
  One command. Every tool. Every monitor.
</p>

<p align="center">
  <a href="https://magent.now"><strong>magent.now</strong></a>
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
  <a href="https://pypi.org/project/magent-multi-ai-agents-manager"><img src="https://img.shields.io/pypi/v/magent-multi-ai-agents-manager?color=3776AB&label=pypi" alt="PyPI version" /></a>
  <a href="https://pypi.org/project/magent-multi-ai-agents-manager"><img src="https://img.shields.io/pypi/dm/magent-multi-ai-agents-manager?color=blue" alt="PyPI downloads" /></a>
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

Each magent session brands its psmux status bar — `magent` on the left, its window hotkeys on the right (`F1 Proj. Picker   F2 </> VS Code`, each key name badged in bold cyan). The `F2 </> VS Code` half only appears when VS Code (`code`) is installed on the session host; without it the bar advertises `F1 Proj. Picker` alone rather than a key that would do nothing:

| Key | In a `magent:` window |
| --- | --- |
| `F1` | Detach the session — back to the picker. |
| `F2` | Open the project's folder in VS Code. When you're attached to another machine it opens over Remote-SSH, so you edit the files where they actually live. |
| `Alt+V` | Paste a clipboard image, or files copied in Explorer, into the session (Windows — see below). |

`F2` needs `code` on your PATH; `magent up` refreshes the branding and hints on sessions that were already running. magent sets both halves per session, so they win over a personal `~/.tmux.conf`.

#### Attach windows reconnect themselves

`magent attach <host>` opens one window per remote session, and each one runs a small supervisor (`magent-attach-client`) instead of a bare `ssh`. When the connection dies — laptop sleep, wi-fi change, VPN flap, host reboot — the pane no longer freezes on `client_loop: send disconnect` and then sits there dead as `[process exited with code 255]`. It waits (2s, doubling to a 30s ceiling) and dials again, forever, until the host answers.

A whole outage costs **one line**, rewritten in place — not a scroll of retries. The counters tick down where they are, and ssh's own `connect to host ... timed out` noise is folded into the `last:` clause instead of filling the pane:

```text
  ~ reconnecting to me@desk (attempt 4, retry in 16s, last: Connection timed out) -- Ctrl+C to stop
```

That line lives on the **bottom row of the pane, and nowhere else**. When the connection dies your agent's screen is left exactly as it was — mid-answer, and with whatever you had typed into the prompt box and not yet sent still sitting there, readable, on the row it was always on. The reconnect warning never draws over it. (It used to: it painted wherever the cursor happened to be, which in an agent pane is the end of your half-written sentence.)

Narrow panes drop the hint, then the host name, then the reason — the attempt and the countdown are the last things to go. Redirected panes (`magent attach ... > log`) get one plain line per attempt instead, with no cursor tricks. When a reconnected session eventually ends, the pane leaves one permanent record of the outage it survived (`+ reconnected to me@desk after 4 attempt(s); stayed up 1h04m`).

Nothing is lost while it waits: the psmux session lives on the **host**, so the reattached pane comes back to the same running agent, the same scrollback, and the same unsent prompt text — the agent is holding it, not your terminal. You can even keep typing during the outage: what you type is buffered by your terminal and delivered to the agent as soon as the connection is back. You do not have to close a wall of dead terminals and re-run `magent attach` any more.

Only a **deliberate detach** closes a pane. When a connection ends, the supervisor asks the host — over a separate, one-shot SSH check — whether your session is still alive. If it is, you left on purpose (`F1`, or `psmux detach`), the pane says `detached from <session>` and exits. If the session is *not* there (host rebooting, a bring-up still in progress), the pane keeps dialling for a few more tries and only then stops and tells you to run `magent attach` — so it never hammers a healthy SSH server over a session that is gone for good. `Ctrl+C` stops the supervisor immediately at any point.

That check exists because an exit code alone cannot be trusted: **Windows OpenSSH doesn't report a remote command's exit status back over an interactive session**, so a session that *died* on the host looked exactly like a clean detach. Panes used to close on that — one wi-fi flap, forty windows gone, each announcing a "detach" you never asked for. The separate check drops the interactive pseudo-terminal, which is what makes the host's answer truthful on every OS.

`magent attach --no-reconnect` restores the old one-shot behavior (one connection, no check). `--no-mux` panes are never supervised — without a multiplexer the agent dies with the connection, so there is nothing to reattach to.

#### Your sessions survive your connection

A dropped connection must never kill work on the host. That is not automatic on Windows: OpenSSH runs everything an SSH session starts inside a *job object* that it destroys when the connection closes, and every child inherits it — so a psmux session created by a remote `magent up` (which is exactly what `magent attach` does) used to be owned by your laptop's wi-fi. One flap and the host's psmux servers, and the agents inside them, were killed. magent now creates sessions with an explicit break-out from that job, so a session's lifetime is tied to the host, not to the connection that asked for it.

Note that `magent down --all` *is* the deliberate way to stop everything: it kills every psmux session on the machine along with the agent running in each, not just the daemons. Name sessions explicitly (`magent down api web`) or use `-g/--group` to stop a subset.

#### The host brings itself up on its own desktop

`magent attach` asks the host to run `magent up` for you, over SSH. On Windows that is a problem nobody sees coming: OpenSSH is a *service*, so every process an SSH login starts lives in logon **Session 0** — a session with no desktop attached to any monitor. Sessions created there are real and running, and completely useless: the host's own `magent status` reports them stopped, nothing can tile or attach to them, and because psmux's session registry is shared they *hold their names*, so every later bring-up on the real desktop fails with "session never came up". (Measured once: 82 psmux servers and 42 agents, plus an upload server squatting the loopback port the desktop's Alt+V needed. Clearing it took an elevated kill of 1172 processes.)

So a bring-up that finds itself in Session 0 does not run there. It hands the same command to the logged-on desktop through Task Scheduler, waits for it, and relays its output back down the SSH pipe — you see the host's normal `up` output on your laptop, prefixed by one `hand-off: ...` line. No password, no elevation, no scheduled task left behind. The same applies to the upload server `attach` ensures on the host. A plain foreground `magent serve` is left alone.

`MAGENT_SESSION0_POLICY` controls it: `handoff` (default), `allow` for a headless Windows host that is only ever reached over SSH and has no desktop to hand off to, or `refuse` to make the situation loud instead. If nobody is logged on at the host's console there is nowhere to hand off to, so the bring-up refuses and says exactly that rather than waiting out a scheduled task Windows is never going to start. Nothing changes on macOS or Linux, where there is no session isolation and tmux over SSH is simply how people work. If servers from an older magent are still stranded, `magent doctor` and `magent status` count them for you.

#### Your typing outranks your fleet

On Windows, magent keeps every psmux process at **above-normal** priority. Your keystrokes reach an agent through a chain of psmux processes, none of which owns a window — so Windows never gives them the boost it gives a foreground app, and under load they queue behind the very builds and language servers they are hosting. That is the difference between typing that feels instant and typing that lags while a normal text box on the same machine stays snappy. The processes are waiting on a pipe rather than burning CPU, so the boost costs your agents nothing.

It needs no administrator rights, it only ever *raises* a process (anything you or another tool put at high/realtime priority is left alone), and it re-runs periodically so sessions created later are covered too. Set `MAGENT_PSMUX_BOOST=0` to leave every process's priority exactly as it is.

#### Idle reaping: a finished agent is parked to free memory

An idle Claude Code still holds its memory. On one measured fleet, 31 agents held 57.6 GB, and the four idle for more than two hours could have given back about 9 GB. So on Windows, `magent serve` checks every five minutes for a local session whose agent **finished its turn** and has sat untouched for longer than `settings.idleReap.afterMinutes` (default 120, never less than 30), and *parks* it: it stops the agent's processes, keeps the pane, its shell, the psmux session and the window exactly where they are, and prints one line in the pane:

```
magent: parked after 120 min idle to free memory. Resume: claude --resume <session id>  (or magent status, r<n>)
```

Resuming picks up the same conversation: type that command at the pane's prompt, or run `magent status` and choose `r<n>` for the session. A resume is always by that exact id, never `--continue`. A bulk revive (`magent up`, and the `up` that `magent attach` runs on the host) leaves a parked session alone, so attaching does not undo the saving.

It is deliberately hard to park the wrong thing. A session is parked only when every reading agrees it is finished and quiet: Claude Code's own status, magent's state record, the transcripts (subagents included) and the screen. A turn waiting on you (a permission prompt, a question), a background subagent or shell still running, or a draft left in the input box each keep it running. A session that shares its folder with another window, a remote or IDE session, a tool other than Claude Code, and anything that cannot be read are left alone: unknown never reads as idle. Each process's identity is re-checked at the moment it is stopped, and the pane's shell is never touched. `~/.magent/logs/reap.log` records every park (with the memory it freed) and every reason a session was spared.

It needs the state hook (`magent hooks install`, see [Where agent states come from](#where-agent-states-come-from)): without its records nothing is ever parked, and `magent doctor`'s `idle-reap` check says so. A parked session reads `parked` in `magent status`, `watch` and `status --json`, and it loses its `[+]` title badge: a finished turn you had not looked at yet stops being flagged once it is parked, so check `magent status` when you come back.

To turn it off, set `"idleReap": {"enabled": false}` (or just `"idleReap": false`) in `settings`, or `MAGENT_IDLE_REAP=0` in the environment or `~/.magent/.env`.

### Mobile file upload (over Tailscale)

Send screenshots, logs, archives — any file — from your phone straight into a project's agent session:

```json
"settings": { "psmux": true, "uploadServer": true, "uploadPort": 8033 }
```

`magent serve` (or `uploadServer: true` during launch) starts a small HTTP server on this config's `uploadPort` — the same port `magent status`, `up` and `doctor` watch, so a bare `magent serve` and the rest of the tool can't disagree about where the server is (`-p` still overrides it; with no readable config the port falls back to 8033). `magent mobile` prints the phone URL + a QR code you can install as a home-screen app (the QR code needs the optional `qr` extra: `pip install magent-multi-ai-agents-manager[qr]`). Pick a project on the phone, upload one or several files of any type, and their paths are pasted into that project's session as one line. On a desktop browser you can also **Ctrl+V** copied files or an image: the page stages everything you copied (a single image with its preview, anything else as a tile naming the files, with their total size) alongside which project it will go to, waits for you to confirm with **Send**, and shows live upload progress until the "pasted into …" confirmation. A file keeps its own name on disk (dotfiles such as `.env` included; a very long name is shortened, extension kept); only a nameless clipboard blob gets a generated `paste-<time>` one. Uploads are capped at **100 MB of files** per send (all the files in it together), and a bigger one is refused with a message naming that limit — by the page before anything is sent, and by the server regardless. Folders are refused here too (`folders not supported - copy files`), and one folder in a selection refuses the whole send. A folder can only arrive by paste or drag-and-drop (the file picker can't select one); the page recognises it by the entry the browser attaches to the pasted or dropped item. Only when the browser attaches no entry does the page fall back to the shape a folder arrives as — an empty file the browser has no MIME type for — so in that case an *empty* `.toml`, `.log`, `.gitkeep` or `Makefile` is refused the same way. The Alt+V hotkey (Windows) does the same for whatever `magent:` session is focused: it takes a clipboard image, or files copied in Explorer (several at once become one pasted line, each path quoted when it needs to be). Copied folders are refused rather than guessed at. On the machine that owns the session, Alt+V pastes the copied files' **original paths** — nothing is copied, uploaded or size-limited, because the agent can already read them where they are; only a remote-wired listener (`magent attach`) uploads them. Setting `MAGENT_ALTV_NATIVE=1` makes a *local* press skip the pipeline and deliver one native Ctrl+V to the pane instead -- opt-in, because the pane's agent must support pasting on an injected Ctrl+V (Claude Code on Windows reacts only to the physical chord, so for it the default upload path is the one that works). Remote-wired listeners (`magent attach`) always keep the upload path.

This works **over Tailscale**: the server binds only the loopback and your machine's Tailscale IP — never the LAN wildcard — and `attach`/`mobile`/`termius` shell out to the `tailscale` CLI to resolve hosts. Devices must be on your tailnet; there is deliberately no auth token, since the bind set is the access control. To bind something else (e.g. LAN-wide), use the escape hatch: `magent serve --host 0.0.0.0`.

#### The Alt+V listener stays alive by itself

The upload server owns the Alt+V listener: while `magent serve` runs it makes sure a listener exists, restarts one that died, one that is alive but wedged (its heartbeat silent for 90 seconds), or one running older code after an upgrade, and leaves alone one that `magent attach` pointed at another machine. So Alt+V survives reboots, crashes and upgrades — start the server (directly, or via `magent --go` / `magent attach`) and the hotkey follows.

If it *isn't* working you will be told, rather than left guessing:

- `magent status` prints `Alt+V listener   DEAD  (upload server is up but no listener — Alt+V does nothing)` in red and **exits 3**, with the repair command underneath. `magent doctor` fails the `hotkey` check with the same hint. A listener that is simply not expected yet (no server running) still reads as a quiet `off`.
- Every press narrates itself in that project's status line, starting the instant the chord is detected: `Alt+V: capturing...` (before the clipboard is even read) → `Alt+V: uploading...` → `Alt+V: image sent` (or `2 files sent`; a local press of copied files is `Alt+V: pasting...` → `Alt+V: 2 file paths pasted`, and a local native press is `Alt+V: pasting...` → `Alt+V: pasted from clipboard`). A press that can't complete ends in a **specific** reason rather than a generic failure — `clipboard has no image or file - copy one first`, `folders not supported - copy files`, `too large - 100 MB limit`, `could not read a copied file - is it open elsewhere?`, `a copied path has a control character - not pasted`, `cannot reach magent serve (connection refused)`, `serve said HTTP 400: Unknown project`, `saved, but psmux would not paste it`. The narration never delays the press: it is queued and delivered on its own thread, and a dead server costs a paste nothing.
- Every press is also recorded in `~/.magent/logs/hotkey.log` as one `ALTV outcome=… project=…` line — so `grep ALTV ~/.magent/logs/hotkey.log` is the whole history of the chord — and `magent serve` logs each status-line message it served (`flash project=… msg=…` in `~/.magent/logs/upload.log`), so "the status didn't show" is answerable after the fact.

To own the listener's lifetime yourself, set `MAGENT_HOTKEY_SUPERVISOR=0`; `status` still reports whether one is running.

#### The attention daemon and the upload server keep each other alive

`magent attention -d` restarts a dead upload server, and `magent serve` restarts the attention daemon. Serve checks as soon as it starts and then every 30 seconds. It restarts a daemon that a reboot took down at its first check, and one that crashed once its heartbeat has gone stale (about a minute), so after a restart the next `magent --go`, `magent up` or `magent attach` brings back badges and flashes along with Alt+V. It never starts a daemon you did not run, or one you stopped with `magent attention --stop` or `magent down --all`. Set `MAGENT_ATTENTION_SUPERVISOR=0` to manage the daemon yourself.

`magent status` tells the cases apart. `CRASHED` (exit 3) means the daemon died while the machine stayed up; the line says when a running upload server will restart it. `not running since the last restart` (exit 0) means a reboot stopped it. A plain `off` means it was never started or was stopped on purpose.

## Nodes

### Three commands

A project can run its Claude session on another Linux machine (a node) instead of this PC. You need two things: ssh access to that machine as root, and one browser Approve on this PC the first time. GitHub access for the node's clones uses this PC's `gh` login; if `gh` is installed but not logged in, `node add` offers to log it in for you (`gh auth login --web`, one more browser Approve). Then:

```bash
magent node add build-box                   # set the machine up as a node
magent config add ~/code/api --node auto    # run this project on a node
magent up                                   # bring it up there
```

`node add` sets the machine up over ssh: it installs what a session needs, creates your user there, authorizes your key and shares your gh login for git. The first time, it runs `claude setup-token` on this PC and your browser opens for the one Approve. The token that approval creates is what node sessions sign in with. Nothing is typed on the node, and no AI agent is needed to finish anything: when `up` returns, the session is running on the node in the project folder, and Claude Code already trusts that folder.

A node that is not ready when you run `magent up` or `magent --go` (never set up, not answering, or without a Claude token while this PC has none to give it) is set up inline at a terminal. You get one question, `Set up @<nick> now? [Y/n]`, and the bring-up goes on. Without a terminal (a daemon, the `up` that `magent attach` runs on a host, a script), nothing is asked or minted. That project is skipped for this run, in one line that names the command that fixes it.

### A project on a pool machine

`settings.nodes` lists the pool machines a project can run on, keyed by nick:

```json
"settings": {
  "nodes": {"second": {"host": "build-box", "user": "alice", "root": "~/magent"}}
}
```

A nick is 1-6 characters of `a-z`, `0-9` and `-` (it is drawn in the status bar); `auto` and `cloud` are not nicks. `user` defaults to your local username at use time; `root` is where project clones live on the node.

A project's `"node": "second"` runs its session on that machine. The node holds a git clone at your current branch, so `magent up` refuses a local tree the node could not reproduce: uncommitted or unpushed work (`--allow-dirty` lets it through, and the node gets origin's copy), no origin, a detached HEAD, or a branch with no commits. `node` is exclusive with `host`. The gitignored files a session needs (`.env*`, `.claude/settings.local.json`, `CLAUDE.local.md`, `.mcp.json`, plus a project's `push` list) are shipped at bring-up.

- `magent node add <host> [--nick N] [--user U] [--key F]` adds a machine to `settings.nodes` and sets it up in one step. It creates the config file if there is none. The nick comes from the host (`gpu-server` becomes `server`; a taken nick gets a digit). An ssh alias is kept as written. The same host added again keeps its nick. A step that fails keeps the node in the config and names `magent node setup <nick>` to finish it.
- `magent node remove <nick> [--local]` takes a machine out of `settings.nodes`. It never touches the machine. A project that would be left with no node (pinned to that nick, or `auto` when it is the last node) runs on this PC instead: at a terminal it lists them and asks `Run them on this PC instead? [Y/n]`, and `--local` does it without asking. Without a terminal and without `--local` it refuses, naming those projects and the one command. It also refuses, with nothing written, while a session still runs there (it names `magent node recall <name> --local`, which brings the conversation home and stops the session; `magent down` would leave the conversation on the node): once the node is removed, magent could no longer reach that session.
- `magent config add <path> --node <nick|auto>` adds a project straight onto the pool. `magent config set <project> node <nick|auto|none>` pins or unpins one later (`none` runs it on this PC again). Both check the whole config first and write nothing it could not load.
- `magent node setup <nick> [--user U]... [--key F]` prepares a machine once. Root is used for this one hop only, to install packages, create a per-person user and authorize your key. Nothing is left for you to do by hand on the node:
  - **Claude runs on your subscription, never an API key.** The first setup runs `claude setup-token` on this PC. You approve once in the browser that opens; the token is kept owner-only in `~/.magent/claude-oauth-token` and reused for every node until it nears the end of its year. Node sessions start with it as `CLAUDE_CODE_OAUTH_TOKEN`, with `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` dropped. Your Claude login (`.credentials.json`) is never copied: its refresh token rotates, and a copy would sign this PC out.
  - **git reaches GitHub over https with your gh login.** With no gh login on this PC, setup asks `Log this PC's gh in to GitHub now? [Y/n]` at a terminal and runs `gh auth login --web` there, then goes on; without a terminal it says so in one line and asks nothing. Setup runs `gh auth setup-git` on the node and points `git@github.com:` / `ssh://git@github.com/` remotes at https. A per-node GitHub ssh key is optional: it is added only when your gh already has `admin:public_key`, and skipped otherwise.
  - It is idempotent: every step prints ok/did/skip.
- `magent node doctor [<nick>]` checks a node: tmux/git/claude/gh on PATH, the Claude subscription token (owner-only, the credential Claude Code would actually use, and a non-billing check that Anthropic accepts it), git's GitHub login, locale, free disk, and the sync daemon's heartbeat and snapshot age.
- `magent node auth refresh` mints a new subscription token (one browser approval) and pushes it to every configured node. A node that does not answer gets it at its next bring-up. `magent node auth status` says whether this PC has one and until when, never the token.
  - The token lasts a year. From 30 days before its end, `magent status`, `magent doctor` and the `magent node` commands say so in one line. At a terminal, the node commands then ask `Renew it now?` (one browser Approve, then every node gets it). `magent status --json` carries its state as `claude_token`: `ok`, `soon`, `expired`, `untrusted` or `none`, or `null` when no node is configured. It never changes the exit code.
- `magent node sync -d [--once] [--stop]` is the daemon that pulls transcripts and agent states home and samples each node's load. `magent serve` starts it while a session is placed on a node. `MAGENT_NODE_SYNC=0` stops serve from doing so (a sync run by hand still runs).
  - It stops by itself 10 minutes after the last placed session goes, and the next bring-up on a node starts it again.
  - It also stops once the config file it reads is gone. While it follows a config other than the one `magent status` reads (one started by `magent --config <file> up`), `status` names that file in one line.

### Placement, plan, push and recall

`"node": "auto"` lets magent pick the machine. It reads each node's load over the last 30 minutes, which the sync daemon samples, never a single reading, so a box used in bursts is not mistaken for an idle one. It penalizes load spikes and low free memory, skips a node under 10% free memory while another is above it, and spreads your own sessions out. A node with fewer than five recent samples gets one live reading (none under `--dry-run`); a node that does not answer it is left out. The choice then sticks: a project stays on its node until that node leaves `settings.nodes`, and the choice is kept in `~/.magent/nodes/node-map.json`, never in your config. `magent --go` and `magent up` place the same way, all of one launch's `auto` projects together so they spread out; an `auto` project that cannot be placed is not launched and its row says why.

- `magent node plan <project|--all>` shows each node's score, which one would be chosen and why, and the files that would be shipped. It changes nothing. A project already placed shows its node and no scores.
- `magent node push <project>` re-ships the gitignored files (`.env*` and the rest) to the project's running session, after you edit `.env` for example. For a cloud project it hands the files off by hand (see Cloud sessions).
- `magent node recall <project> --local` brings a session home. It pulls once more, prints the node's last commit per repo and whether its tree was dirty, stops the session on the node (and prints the command that does when it cannot), installs the conversation and its memory (the memory alone when no conversation was pulled) into this PC's Claude directory under the project's local folder (resolved exactly as `--go` resolves it), and prints what to run: the `git pull`, then `cd "<folder>"` and `claude --resume <id>` as two lines, never joined by `&&` (plain `claude` when no conversation was pulled, and a `cd /d` reminder for cmd.exe when the folder is on another drive letter). The copy follows no links, refuses a pulled folder that is itself a link, skips the pull's unfinished `.part` files, and names every local file the node's copy changed. It ends with: "@<nick> keeps its copy: a later bring-up there continues the node's conversation, not the turns added here."
  - A node that does not answer at all (unreachable, or timed out) is reported, not fatal: what was already pulled is used, and the command that stops the session there is printed.
  - A node that answers but whose last pull did not finish (it answered with an error, some files did not land, or its placement was not found again) stops the recall before anything is stopped, installed or cleared: it exits 1, the project stays placed, and it tells you to run the recall again. A pull stuck at its mark (the node keeps answering from the same point) is the one exception: a re-run would get the same answer, so it names nodes.log, where both marks are, instead. A node map another process holds busy exits 1 the same way. A torn or otherwise unreadable one, a malformed entry in it included, exits 1 too, and names the file to fix or move aside before the re-run (no magent command rebuilds it); a sync daemon that keeps pulling from that node past the wait exits 3, also with nothing touched. A folder of pulled conversations this PC cannot list exits 1 the same way and names the folder: it is never read as "nothing was pulled".
- `magent node recall <project> --to <nick>` moves an `auto` session to another node and resumes the same conversation there; what was pulled is installed there even when it holds no conversation, only memory, and the session then starts fresh. A pinned project moves by changing its `"node"`. Before anything is touched it refuses (exit 2) a move the new node could not take, with the checks `magent up` makes: a node folder name another project shares, or a local tree the node could not reproduce (uncommitted or unpushed work, no origin, a detached HEAD, a branch with no commits). `--allow-dirty` works as it does for `magent up`: it lets uncommitted or unpushed work through (the node gets origin's copy), and the other three are still refused. Once the session runs on the new node, the node sync daemon is started as after any bring-up.

A session brought up again on the same node continues its newest conversation there (`claude --continue`); only a recall picks a conversation by id.

A `node-map.json` magent cannot read, torn or with one malformed entry, pauses the node sync: no node is pulled until the file reads again. `magent status` shows `node sync paused` under Nodes, naming the malformed entry, with the file to fix or move aside, and `status --json` carries it as `node_sync_paused`. It is degraded, as a stale sync daemon is: `magent status` exits 3 until the map is repaired. A map that is only busy (another process is writing it) is not a pause.

### Cloud sessions

`"node": "cloud"` runs a project's agent in a Claude Code cloud session instead of on a machine you own. magent opens it as a local psmux pane that runs `claude --cloud "<cloudTask>"`, branded `@cloud` in the status bar. The project runs the `claude` tool, needs `cloudTask` (the task the session starts on), and needs psmux (Windows).

- **One session per create.** `claude --cloud "<task>"` starts a NEW cloud session every time, so magent types the command once: `up` never re-sends it and `revive` skips the pane. `down` closes the local pane only. The cloud session keeps running, then pauses when idle and is reclaimed later; archive or delete it at claude.ai/code.
- **Git is the truth.** The cloud clones your GitHub remote at the checkout's branch. magent refuses the create for a checkout with no GitHub remote, a detached HEAD, or uncommitted or unpushed work. Untracked files never reach the session.
- **`.env` goes first.** A clone never contains gitignored files. `magent node push <project>` hands them off by hand: names print masked as `NAME  ******** (n chars)`, and the values go to a private temp file you paste into a personal environment's variables at claude.ai/code. A line that could be part of a multi-line value is withheld and counted as `(N line(s) not shown)`; a file that cannot be read shows as `<path>  (could not be read)`. The create waits until the current files have been handed off.
- **Your account.** `--cloud` needs a claude.ai login (`/login`; a `claude setup-token` token does not authorize it). It is unavailable on Bedrock, Vertex and third-party providers, and when your organization's `allow_remote_sessions` policy is off. Pick your environment once with `/remote-env`. `magent doctor` checks the parts this PC can see.
- **Coming home.** `magent node recall <project> --local` prints `cd`, `git pull` and `claude --teleport <id>`. Teleport needs the same repository (not a fork), the branch pushed and the same account, and offers to stash a dirty tree.
- **Refused, with a reason.** Alt+V and the phone upload page refuse a cloud pane, because the image would land on this PC. `send` and `model` refuse it, because the pane is a viewer: use `claude -p "<msg>" --cloud <session-id>`. `attach --no-mux` refuses it, because it would start a second session.
- **One pane per name.** If a cloud project and a local project end up with the same session name, only the first enabled one in your config starts. `magent status` and `magent doctor` name the one left out.
- **What the surfaces can and cannot tell.** magent cannot list cloud sessions and keeps no record of what it typed. A live cloud pane sitting at a bare shell therefore reads "cloud, start unconfirmed" in `status`, never idle or healthy: it may never have been typed into, or the command may have run, handed off and returned to the shell. After psmux itself crashes, look at claude.ai/code before `magent up`: magent cannot see that the cloud session is still running, so the next create starts a NEW one.
- **JSON.** The additions are all optional keys. `sessions --json` carries `node` on every row (`"cloud"` for a cloud pane) and the state `"cloud"` for a pane magent does not read. `status --json` puts `node: "cloud"` on a cloud row of `psmux_sessions`, and carries top-level `shadowed_cloud` / `shadowed_local` lists (`[{session, path, why}]`) when a project was left out. `up --json` carries `node` on every `projects[]` entry (`"cloud"` or `null`); its `up` and `down` result entries carry `node: "cloud"` on cloud rows, a `note` on a live local pane that shares a cloud pane's name, and a `reason` on `down` entries.

A cloud session cannot reach MCP servers that run on this PC.

## Usage

Run `magent` with no arguments for the interactive menu:

```
                         _
 _ __  __ _ __ _ ___ _ _| |_
| '  \/ _` / _` / -_) ' \  _|
|_|_|_\__,_\__, \___|_||_\__|
           |___/
  v3.1.1  auto-tile your AI workspace

  ----------------------------------------

   1   Launch & tile new windows  (default)
   2   Re-tile all open windows
   3   Launch a group  AUTOMATIONS | INTERNAL | LEAD-GEN
   e   Edit config
   q   Quit
```

**Just start typing.** In a real terminal every list magent shows you — the
menu above, its group submenu, and the `magent sessions` switcher — filters as
you type, with the closest match marked `>`:

```
  attach to web

 > 2   beta-web                   still going... 4m
   3   gamma-web-docs             needs input
```

Up/Down move the mark, Enter takes it, Esc clears the query (and, on an empty
query, backs out). Nothing else changed: the row numbers still work, `q` is
still Quit even if you have a project called `queue-worker`, and pressing Enter
on an untouched menu still takes the default it always did. Piped or scripted
input keeps the plain line-based prompt.

Or skip the menu with flags:

| Command | What it does |
| --- | --- |
| `magent` | Interactive menu. |
| `magent --go` | Launch + tile new windows, no menu. On a terminal it first asks **which** projects (see below). |
| `magent --go --all` | Same, but launch every enabled project with no checklist (`-a` for short). |
| `magent --retile-all` | Re-tile every magent window that is open right now — including `magent attach` windows, which belong to a remote host's sessions and are in no local project. Launches nothing; a closed window is skipped, not waited on. |
| `magent -g <name>` | Launch only projects in a group. |
| `magent --init` | Re-scan sessions and regenerate config. |
| `magent --init --base-dir <folder>` | Generate config from a folder of git repos. |
| `magent --edit` | Open config in your default editor. |
| `magent docs` | Print full config reference (Markdown). |
| `magent doctor [--json]` | Diagnose the environment: config, env vars, agent tools on PATH, terminal, a wedged psmux control plane (see below), monitors, writable dirs, Tailscale, upload port, idle reaper. Exit 1 on any failure. |
| `magent sessions` | List active psmux sessions, pick one to attach. |
| `magent sessions <name>` | Attach directly to a psmux session by name. |
| `magent sessions --json` | Print every configured session as JSON — name, cwd, a live flag, and (for live ones) the model, effort, and state read from the pane. Non-interactive; attaches nothing. |
| `magent send <session> "<text>" [--file f] [--wait-idle] [--compact] [--timeout s]` | Type a prompt into one running agent by name and submit it. Resolves the name case-insensitively (exact, then unique substring/prefix); refuses if it is not live. Exit codes: 0 sent, 2 not found, 3 psmux error, 4 not confirmed. See [below](#driving-a-session-from-another-shell). |
| `magent model <session\|--all> <model> [--effort low\|medium\|high\|xhigh\|max]` | Switch a session's model (and optionally effort) while it is idle, retrying busy sessions until `--max-minutes`; prints a per-session before/after table. |
| `magent peek <session> [-n <lines>]` | Print the last N pane lines of a session — a read-only glance. |
| `magent up [--json] [-g <group>] [--revive]` | Host side: ensure a persistent psmux session per project, and re-launch the agent in any live session whose pane fell back to a bare shell (e.g. after a Ctrl-C). Reviving is automatic except under `--json`, which stays a pure read unless `--revive` is passed. A session the idle reaper parked is never revived in bulk (see [Idle reaping](#idle-reaping-a-finished-agent-is-parked-to-free-memory)). |
| `magent attach <host> [--no-reconnect]` | From another PC: bring host sessions up over SSH, tile locally, Alt+V uploads, F2 opens the project in VS Code over Remote-SSH. Panes reconnect themselves after a dropped connection (see below); `--no-reconnect` opts out. |
| `magent watch` | Live table of every agent session, most-urgent first; press a row number to focus that window. |
| `magent attention [-d] [--stop]` | Attention daemon: badges window titles with agent state, flashes the taskbar on needs-input/error, optional toast/ntfy push (`settings.attention`). Badges/flash/toast are Windows-only; ntfy push is cross-platform — see [Platform support](#platform-support). |
| `magent status [--json]` | Session + daemon health (incl. an `agents` state list in `--json`). Exit codes: 0 healthy, 1 config error, 3 degraded. |
| `magent down [--all] [--server] [--host <host>]` | Stop sessions; `--all`/`--server` also stop the upload server (and listener). From an attach client there are no local sessions, so the shutdown is forwarded over SSH to the host you last attached to (or to `--host`), closing the local attach windows first. |
| `magent serve [--host <addr>]` | Run the mobile upload server (see below). |
| `magent mobile` | Phone URL + QR code for installing the uploader as a home-screen app. |
| `magent termius` | Generate an SSH config entry that opens the session picker. |
| `magent hotkey [--ssh-host <host>]` | Run the window-hotkey listener standalone (Windows): Alt+V clipboard upload and F2 open-in-VS-Code. `--ssh-host` makes F2 open over Remote-SSH. |
| `magent hooks install` | Wire the agent lifecycle hooks that feed the session-state store (`magent hooks status` to inspect) — see [Where agent states come from](#where-agent-states-come-from). |
| `magent terminal install` | Bind Ctrl+Backspace and Shift+Enter in Windows Terminal so they still work inside a psmux pane (`magent terminal status` to inspect) — see [Typing through psmux](#typing-through-psmux). |
| `magent config <subcommand>` | Edit config from the CLI — 17 subcommands incl. `migrate`; see `magent config --help`. |
| `magent config edit [host]` | Edit the config on **another** machine in your editor over SSH — fetch, edit, validate, push back. Omit the host to reuse your last `attach` target. The host side is `magent config cat` / `magent config put`, which you never run by hand. |

### Choosing what to launch

A fleet grows, and most launches want four of its fourteen windows. So `magent --go` (and the menu's **Launch & tile new windows**) asks first, on a real terminal, with **everything already checked** — pressing Enter is exactly the old "launch them all":

```
  Launch which projects?
  ----------------------------------------

  work
  >  1  [x] api-gateway
     2  [x] web-app
     3  [ ] admin-console

  other
     4  [x] scratch

  3 of 4 selected
  space toggle   a all   n none   g section   up/down move   enter launch   q cancel
```

Up/Down (or `j`/`k`) move, **Space** toggles the row, **`a`**/**`n`** check or clear everything, **`g`** toggles the whole section the cursor is in, digits **1-9** toggle that numbered row, **Enter** launches the checked set, and **`q`**/Esc walks away with `Nothing launched.` (as does Enter with nothing checked). Projects are grouped by their `group` field; ungrouped ones sit last under `other`.

Off a terminal — a script, cron, CI, anything piped — there is **no prompt at all** and every enabled project launches, exactly as before. `--all` (`-a`) is the same escape hatch when you *are* on a terminal. `-g <group>` narrows the checklist to that group, and `--retile-all` never asks, since it launches nothing.

### Driving a session from another shell

`magent send`, `magent model`, and `magent peek` turn the fleet into something
you can script — an API-ish way to talk to a specific agent, or all of them,
without switching windows. They build on the same psmux plumbing everything
else here uses.

```bash
magent send caramel "Continue the release; be token-efficient."
magent send caramel --file prompts/caramel.txt        # long prompt from a file
magent send caramel --compact "New task..."           # /compact first, wait, then send
magent send caramel --wait-idle "Next step"            # hold until the agent is free
magent peek caramel -n 60                              # look without touching
magent model caramel opus --effort high                # switch one session
magent model --all fable --effort high                 # put the whole fleet on one model
magent sessions --json                                 # machine-readable fleet state
```

`send` pastes the text **literally** (`send-keys -l`) and then presses Enter as
a separate key, so the whole prompt lands on one input line and submits once.
It confirms the prompt actually left the input line before reporting success,
and its exit codes (0/2/3/4) make it safe to drive from a script. `model` only
switches a session while it is **idle** — never mid-turn — and re-reads the
`<Model> · <effort>` footer to verify the change took, retrying anything busy
until `--max-minutes` runs out. All three resolve a session name
case-insensitively and refuse a name that is not live. A pane psmux does not
answer within a few seconds (a loaded box) is reported as unread, never as
empty: `send` exits 4 (not confirmed), `peek` exits 3, and `sessions --json`
shows `"state": "timeout"` rather than `"nopane"`.

> The slash-commands `send`/`model` issue (`/compact`, `/model`, `/effort`) are
> built inside magent and handed to psmux as a list argument, never through a
> shell — so Git Bash / MSYS can't rewrite a leading `/model` into a Windows
> path. Typing one yourself as a prompt is different: in Git Bash, `magent send
> caramel "/compact"` reaches magent as `C:/Program Files/Git/compact`, because
> the MSYS runtime rewrites the argument before magent starts and quoting does
> not stop it. Use the `--compact` flag, or set `MSYS_NO_PATHCONV=1` for that
> command.

### Typing through psmux

Two keys stop working the moment your agent runs inside a psmux pane:

- **Ctrl+Backspace** arrives as a plain Backspace — one character, no word-delete.
- **Shift+Enter** arrives as a plain Enter — which *submits* in Claude Code
  instead of inserting a newline.

Neither is your terminal's fault. psmux drops the key **modifier** in transit;
the child only ever sees the bare key. The real fix upstream is win32-input-mode
(psmux#159), which died unmerged — we filed psmux#610 and #611 to revive it.

Until then the mitigation is to resolve the chord **before psmux sees it**, in
Windows Terminal itself, with a `sendInput` binding that writes the resulting
bytes straight into the pty — a byte has no modifier left to lose:

```
magent terminal install     # writes the bindings (backup first, never clobbers)
magent terminal status      # installed / missing / conflicting, per key
```

- `ctrl+backspace` → `0x17`, the Ctrl+W word-erase byte every readline already
  honors. The same trick VS Code ships. Works through psmux **today**.
- `shift+enter` → `0x1b 0x0d` (ESC CR), exactly what Claude Code's
  `/terminal-setup` installs. Works outside psmux now, and inside it once
  upstream fixes its ESC+CR decode — installing it is right either way.

magent ships this itself because `/terminal-setup` **refuses to run inside a
tmux/psmux pane**, which is precisely where magent users live.

The install is idempotent and never clobbers: a key you have already bound to
something else is reported and left alone (the other key still installs), and a
timestamped backup lands beside `settings.json` before any write. If your
`settings.json` uses JSONC (comments, trailing commas) magent refuses to rewrite
it and prints the exact snippet to paste by hand instead. `magent doctor`
reports the same per-key state under `wt-keys` — as a warning, never a failure.

### When every psmux command hangs (the wedge)

Rare, and worth knowing before it happens: psmux's control plane can wedge
machine-wide. Every command — `has-session`, `list-sessions`, `new-session` —
hangs forever, from any console, and the whole fleet looks dead.

It isn't. `magent doctor` probes the control plane once (bounded, 5 s) and
fails the `psmux wedge` check with the repair:

- your sessions are **frozen, not dead** — do not restart them, and do not
  reboot;
- find the `conhost.exe` processes whose parent chain reaches a dead pid or a
  `psmux.exe`, and kill only those (it was 14 of 874 conhosts in the incident
  this check comes from);
- psmux answers again immediately afterwards, and every session comes back
  intact.

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
| Idle reaping (`settings.idleReap`) | Yes | No | No |
| Global Alt+V clipboard image/file hotkey | Yes | No | No |
| psmux-safe keybindings (`terminal install`) | Yes | No | No |
| Mobile upload server (`serve` / `mobile`) | Yes | Yes | Yes |

Notes:

- **Badges, flash, and toast** are gated on `Platform.supports_attention_signals()`, which returns `True` only in `platform/windows.py`. On macOS/Linux the daemon prints `window badges/flash aren't supported on this OS` and those renderers stay off. Toast additionally uses the Windows-only `winotify` (`[toast]` extra). **ntfy push is cross-platform** — it is stdlib `urllib` over HTTP — so phone notifications work on every OS.
- **The `magent:` title prefix** can be turned off with `settings.windowTitlePrefix: false` — window titles then become the bare project name (e.g. `api` instead of `magent:api`). Launch-path tiling still places windows (it matches the exact title it set), but the features that read the `magent:` grammar degrade to a safe no-op while the prefix is off: the attention daemon's title **badges**, the **Alt+V** clipboard hotkey (which only fires in `magent:`-titled windows), and `magent-name` title matching all stop recognizing your windows. One deliberate exception: `magent attach` windows always keep the prefix — there the title carries the psmux session id that the hotkey chain resolves, so it is load-bearing rather than cosmetic. Leave the setting on unless you specifically want prefix-free titles.
- **Persistent psmux sessions and the Alt+V hotkey** are gated on `supports_psmux()` / `supports_hotkey()` (also Windows-only). Off Windows the psmux entry points raise `NotImplementedError` and importing `hotkey` raises `ImportError`.
- **Idle reaping** is gated on `supports_psmux()` and an interactive logon session (`logon_session_is_interactive()`). Off Windows, or in a `serve` running in Session 0, the reaper logs `idle reaper off: <reason>` to `~/.magent/logs/reap.log` once and never sweeps.
- **`magent terminal install`** is gated on `supports_wt_keybindings()` — it edits Windows Terminal's own `settings.json`, which no other OS has. Elsewhere it says so and does nothing (see [Typing through psmux](#typing-through-psmux)).
- The **mobile upload server** itself (serving the PWA over loopback + Tailscale and receiving files) runs everywhere; auto-pasting the uploaded path into a *live* agent session uses psmux, so that last hop is Windows-only. Likewise, `watch`'s table renders on every OS but its press-a-number-to-focus action uses the same Windows-only window primitives.

## Where agent states come from

`magent sessions`, `magent watch`, `magent attention`, and `magent status --json` do not poll your agents directly. They read per-session **state records** — `working`, `needs-input`, `done`, `error`, `idle` — that your coding agent writes through its lifecycle hooks, plus `parked`, which is written by magent's [idle reaper](#idle-reaping-a-finished-agent-is-parked-to-free-memory), not by the hooks. Until those hooks are wired, the state store stays empty and the pickers show no status. Wire them once with:

```bash
magent hooks install
```

This merges the bundled `magent-state-hook` writer into Claude Code's `~/.claude/settings.json` (idempotent — your existing hooks are preserved) and prints the one-line `notify` recipe for Codex's `~/.codex/config.toml`. Restart open agent sessions to activate. After that the session picker shows live `still going... 3m` / `done` / `needs input` labels — the hook refreshes the record on every tool call, so `still going...` means the agent really is moving, and a session reads `done` only when its turn ends with no background tasks (subagents, background shells) still running — and `magent watch` / `magent attention -d` light up as your agents change state. `magent hooks status` shows what is wired and how fresh the store is.

The companion [`ai-agent-notifier`](https://www.npmjs.com/package/ai-agent-notifier) package (same authors) adds phone/desktop notifications on top of the same hook events (it is a pure notifier — it does not write state records):

```bash
npx ai-agent-notifier setup
```

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
| `cloudTask` | none | Required when `node` is `"cloud"` (the project then runs the `claude` tool): the task the cloud session starts on. 1-200 characters starting with a letter or digit: letters, digits, spaces and `, . _ / : -`. It must not look like a session id or URL. A cloud project without it loads, and the create is refused with a named reason. |

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
