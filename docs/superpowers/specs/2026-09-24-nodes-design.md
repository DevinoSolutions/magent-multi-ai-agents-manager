# Nodes: run a project's agent on a pool of machines, with git as the source of truth

**Status:** design approved in conversation 2026-09-24, awaiting spec review
**Branch:** `feat/nodes` (long-lived feature branch; main stays small and proven)
**Release plan:** ship `3.20.0` on a green local full gate, let CI run after, cut `3.20.1` for anything CI finds (user's explicit call for this feature; the standing "all checks green before merge" rule is suspended for this branch only)

---

## 1. Problem

This PC runs ~60 agent sessions and is out of RAM and CPU. The org has Linux boxes
(`box-second` … `box-fifth`, ssh already working) with spare capacity. We want a
project to run *there* — the whole Claude Code session, under tmux — while this PC keeps
being the place you look at it from, the phone still reaches it, and nothing is lost when a
box dies.

Two remote mechanisms exist today and neither fits:

- `ProjectConfig.host` runs the agent over a bare `ssh -t` with no multiplexer; nothing
  survives the connection, nothing is durable, nothing shows on the phone.
- `magent attach <user@host>` expects a **full magent install with its own config** on the
  remote and treats that config as the truth. Here the truth must stay on this PC.

## 2. Decisions (all made with the user; do not relitigate)

| # | Decision | Why |
|---|---|---|
| D1 | **Git is the truth.** The node holds a `git clone` of the project's repo(s) at the branch you are on. No mirror, no rsync, no two-way sync, no lock on the local folder. | Removes the whole cross-OS mirror bug class (native deps, exec bits, CRLF, cloud-sync placeholders, cwRsync). Work comes home the way it already does: the agent commits and pushes. |
| D2 | **Node-only data comes home continuously, one way.** Claude transcripts + the node's `~/.magent/state/` are pulled every 30 s into `~/.magent/nodes/`. | Durability + resume-anywhere without conflicts (append-only data). |
| D3 | **Explicit + auto-detected non-git inputs are pushed at bring-up**: gitignored `.env*`, `.claude/settings.local.json`, `CLAUDE.local.md`, ignored `.mcp.json`, and the project's auto-memory dir. `push: [...]` adds extras. | A session can't work without them; keys/certs stay opt-in. |
| D4 | **Dedicated per-person Unix user on each node** (`alice`, `bob`), created by `magent node setup`; root is used for that one hop only. magent refuses to run sessions as `root` unless `"user": "root"` is written explicitly. | Credential isolation on shared boxes; blast radius; attribution; matches Claude Code's per-user state. |
| D5 | **User-scope auth and config are provisioned one way at every bring-up**: `gh` token, Claude `settings.json` (hooks filtered to what exists on the node), user-scope MCP servers **and their OAuth entries**, plugin list, skills dir. **The Claude login itself is manual per node** (its refresh token is single-holder; two machines refreshing it log each other out — the ccswap failure mode). | "Transfer authentication where it matters" (user), minus the one transfer that would corrupt the source. |
| D6 | **Per-node git identity**: `node setup` generates an ed25519 key on the node in the user's home (never leaves the box) and registers it to the user's GitHub account via `gh ssh-key add`. Agent forwarding is *not* relied on. | Pushes must work with no window attached, after sleep, and from a Session-0 bring-up. |
| D7 | **Refuse a dirty or unpushed local tree** at bring-up (`--allow-dirty` overrides, and then those changes simply are not on the node). | Git is truth; invisible local edits would collide with the node's pushes. |
| D8 | **Placement:** `node: "<nick>"` pins; `node: "auto"` picks by **load history**, not one sample, and sticks. | A bursty box someone else is using must not be piled onto because it was quiet for one second. |
| D9 | **Reuse the attach supervisor unchanged.** The local window is `magent-attach-client` with a new `--mux tmux`. | It took a long time to get right; one module owns reconnect. |
| D10 | **One tmux server per node user** (`tmux -L magent`), sessions named by sid; every `-t` target is the exact form `=<sid>` (bare `-t` is a prefix match on a shared socket). | Termius: `tmux -L magent attach` lands in the most recent session; `Ctrl+B s` is the picker over all of them. |
| D11 | Nothing about prod boxes in code. `root@prod-box` is simply not in the pool. | YAGNI (user). |
| D12 | Deferred, ledgered in DESIGN.md: Alt+V paste into node panes, `send`/`model`/`peek` for node sessions, account routing for node sessions. The `cloud` (Claude Code Web) backend was un-deferred on 2026-09-24 and is §18 (plan J): a LOCAL psmux pane running `claude --cloud "<cloudTask>"`, pin-only, recall by `--teleport`; no CLI/API sets cloud environment variables (verified), so `.env` reaches the cloud through three tiers behind one create gate: a Pro/Max API credential, an `age`-sealed tar on `refs/magent/sealed` of the project's private repo unsealed by a synced plugin hook (default), or a masked manual handoff (§18, DECISION-18). The cloud relay stays deferred (DECISION-16). | Ship the core fast. |

## 3. Vocabulary

- **node** — a pool machine, identified by a **nick** (`second`), with `host`, `user`, `root`.
- **node project** — a `ProjectConfig` with `node` set. Exclusive with `host`.
- **sid** — the session id, `psmux.session_name(title)`; also the tmux session name and the window title body (`magent:<sid>`). Unchanged grammar.
- **remote dir** — `<root>/<repo-name>` on the node (workspace: `<root>/<workspace-name>/<child-repo>`).
- **recipe** — everything needed to run a project somewhere else: `{repos[{url, branch, remote_dir}], push_files, user_scope}`. The node backend consumes it now; the future `cloud` backend consumes the same tuple.

## 4. Configuration (schema v4 → v5)

```jsonc
{
  "version": 5,
  "settings": {
    "nodes": {
      "second": { "host": "box-second", "user": "alice", "root": "~/magent" },
      "third":  { "host": "box-third" },
      "fifth":  { "host": "box-fifth" }
    },
    "nodeSync": { "pullIntervalS": 30, "sampleIntervalS": 60, "historyH": 24 }
  },
  "projects": [
    { "path": "C:/…/sendly", "node": "second" },
    { "path": "C:/…/marka",  "node": "auto", "push": ["apps/web/gcp-sa.json"] }
  ]
}
```

`config.py` additions (typed, validated in `load_config`, defaults in `default_config`/`settings_to_dict`, drift-pinned by `test_config_factory` + the example file):

```python
@dataclass(frozen=True)
class NodeConfig:
    nick: str                 # key of the dict; ASCII [a-z0-9-], 1..6 chars (status-bar budget)
    host: str
    user: str | None = None   # None -> env.local_username() at use time (never persisted)
    root: str = "~/magent"

@dataclass(frozen=True)
class NodeSyncConfig:
    pull_interval_s: int = 30
    sample_interval_s: int = 60
    history_h: int = 24

# ProjectConfig gains:
node: str | None = None      # a nick, or "auto"
push: list[str] | None = None
```

Validation (all in `load_config`, reported through the existing warning/error path):
- `node` and `host` both set → error.
- `node` not `"auto"` and not a configured nick → error naming the nicks.
- `nick` longer than 6 or non-ASCII → error (the status bar is cell-counted; see §9).
- `user == "root"` → warning `nodes.<nick>: running sessions as root; see D4` (allowed, loud).
- `settings.nodes` empty and any project has `node` → error.

Migration `_migrate_4_to_5`: version stamp only; no field rewrite. `SCHEMA_VERSION = 5`.
`REQUIRED_KEYS`/allowed-settings sets gain `nodes`, `nodeSync`. `cli/docs.py` gains the two
tables. `.env.example` gains `MAGENT_NODE_SYNC` (§10).

## 5. Module map — what is new, what is touched, what is reused

New leaves (import policy: stdlib + the existing leaves `log`, `env`, `paths`, `config`, `procs`, `lockfile`, `attach_client`, `psmux` constants — each row's "Depends on" column is the authoritative per-module list (DECISION-19); **never** `cli`, `launch`, `upload_server`, pinned by an AST test per module):

| Module | One purpose | Depends on |
|---|---|---|
| `nodes.py` | Pure data + policy: `Node`, `Recipe`, placement scoring, node-map read/write, snapshot/history file layout under `~/.magent/nodes/`. No subprocess. | `config`, `env`, `paths`, `log` |
| `remote_mux.py` | **The single owner of every subprocess against a node**: ssh argv, `tmux -L magent …`, git-on-node, the shipped shell scripts, tar pull. Every function returns data or raises `RemoteError`; no `sys.exit`, no printing. | `attach_client` (for `SSH_CONNECTION_OPTS`), `psmux` (status constants), `env`, `log` |
| `node_scripts/` (package data) | `setup.sh`, `provision.sh`, `bring_up.sh`, `pull.sh`, `sample.sh`, `state_hook.sh`. Each is `bash -s -- <args>` fed over stdin by `remote_mux.run_script`. POSIX-bash, `set -euo pipefail`, no distro assumptions beyond apt for `setup.sh`. | — |
| `node_sync.py` | The daemon loop (`run_sync_loop`): per tick, one ssh per node doing pull + sample + session list; writes snapshot files; heartbeat. Mirrors `attention.run_attention_loop`'s shape. | `nodes`, `remote_mux`, `log`, `lockfile`, plus the leaves `config` (reload), `procs` (`pid_alive`), `attach_client` (`TMUX_SOCKET`), `env` (`MAGENT_NODE_SYNC`); never `cli/`, `launch` or `upload_server` (DECISION-19) |
| `cli/node_cmd.py` | `magent node` group: table, `setup`, `doctor`, `plan`, `push`, `recall`, `sync -d`. Exit codes and tables live here per the subsystems-return-data rule. Heavy imports in-body per policy. | `cli/app`, `cli/config_io`, `nodes`, in-body `remote_mux`/`node_sync` |

Touched (minimal, each a named seam):

| File | Change |
|---|---|
| `config.py` | §4. |
| `attach_client.py` | `--mux {psmux,tmux}` (default `psmux`) parameterizes `remote_attach_command(sid, mux)`, `_probe_session(...)` and nothing else. `SSH_CONNECTION_OPTS` unchanged. |
| `cli/attach.py` | Lift the wt spawn into `spawn_attach_window(target, sid, *, mux, remote, extra_ssh_opts=()) -> int` (returns pid); `_attach_markers(sid, mux)` adds the tmux marker `-L magent attach -t <sid>`. `attach` itself calls both with `mux="psmux"` — behaviour byte-identical (pinned by the existing attach tests before the lift). |
| `launch.py` | `_dispatch_cli_agent_project` gains a third branch: `if proj.node: return _dispatch_node_project(...)` which builds the recipe (`nodes.recipe_for`), calls `remote_mux.bring_up`, then `spawn_attach_window`. Node projects are excluded from psmux eligibility (like `host` projects) and from account routing (D12). `--dry-run` prints the recipe and touches nothing (same law as routing). |
| `psmux.py` | `status_brand(nick: str | None) -> tuple[str, str]` returns the format string + its cell length; `_STATUS_BRAND`/`_STATUS_BRAND_LEN` become the `nick=None` case. `decoration_argv` unchanged in behaviour. `eligible_projects`/`config_sessions` skip `node` projects. |
| `psmux.bring_up` (`magent up`) | For node projects, delegates to `remote_mux.bring_up` and reports in the same `(created, failed)` shape; `up --json` gains `node` per project. |
| `cli/status.py` | `status`/`down`: node sessions listed from the daemon snapshot with a `node` column; `down` calls `remote_mux.kill_session` after a final pull; `down --all` includes node sessions and stops the daemon. Exit-code 3 (degraded) gains "node sync daemon stale". |
| `cli/session_picker.py` / `sessions --json` | `node` field; node liveness from the snapshot. |
| `hotkey.py` / F2 | For a node project, F2 opens VS Code Remote-SSH (`launch_vscode` with `remote="ssh-remote+<user>@<host>"`, already supported by all three backends). |
| `upload_server.py` | `_supervise_node_sync` daemon thread (same shape as `_supervise_hotkey`): `launch.ensure_node_sync()` every `NODE_SYNC_SUPERVISE_INTERVAL_S`; gated on `MAGENT_NODE_SYNC` and on "any project has `node`". |
| `env.py` | `MagentEnv.node_sync: bool = True` (`MAGENT_NODE_SYNC`); `local_username()` accessor (`USERNAME`/`USER`). |
| `attention.py` / `agent_state.py` | `read_state` gains an optional extra store root; the engine reads `~/.magent/nodes/<nick>/<sid>/state/` alongside the local store, keyed by the node's remote dir → project via the node-map. |
| `cli/doctor` | `nodes` check (runs `node doctor` per node, WARN-at-worst). |
| `DESIGN.md` | §2 "The node is a checkout, not a mirror" (rationale for D1–D10) + ledger entries for D12. |
| `CLAUDE.md` | Commands/architecture/laws bullets for nodes; the `MAGENT_NODE_SYNC=0` test-isolation law. |

**Explicitly not duplicated** (the review checklist for every PR on this branch):

- ssh option list: only `attach_client.SSH_CONNECTION_OPTS`.
- reconnect/probe/outage line: only `attach_client.supervise`.
- wt spawn + `--suppressApplicationTitle` + corpse markers: only `cli/attach.spawn_attach_window` / `_attach_markers`.
- status-bar strings and cell arithmetic: only `psmux.status_brand`/`status_hints`.
- session naming: only `psmux.session_name`.
- start command: only `sessions.build_start_command`.
- "pin is config, assignment is machine state": same file pattern as `account-map.json`.
- daemon pid/heartbeat/lock: `log` heartbeat helpers + `lockfile.exclusive_lock`, same as `attention -d`.

## 6. `remote_mux.py` — the one seam to a node

```python
class RemoteError(RuntimeError):
    """Carries rc, stderr tail, and the *redacted* command (never file contents/tokens)."""

@dataclass(frozen=True)
class Node:            # built by nodes.resolve(config, nick)
    nick: str; host: str; user: str; root: str
    @property
    def target(self) -> str: return f"{self.user}@{self.host}"

MUX = "tmux"; SOCKET = "magent"           # tmux -L magent

def ssh_argv(node, remote_cmd: str, *, tty=False, batch=True) -> list[str]
def run(node, argv_remote: list[str], *, timeout_s, input_bytes=None) -> CompletedProcess
def run_script(node, script: str, args: list[str], *, timeout_s, stdin=None) -> CompletedProcess
    # ssh <target> 'bash -s -- <args>' with the packaged script on stdin (+ optional payload after a sentinel)

# tmux
def has_session(node, sid) -> bool | None          # None = probe failed (unknown), never "False" on a network error
def list_sessions(node) -> list[str]
def new_session(node, sid, cwd, argv: list[str], env: dict[str,str]) -> None   # LANG=C.UTF-8 always
def kill_session(node, sid) -> None
def decorate(node, sid, nick) -> None              # status-left/right/F1 via psmux.status_brand/status_hints
def send_keys(node, sid, *keys) -> bool            # bounded like psmux.send_keys; used only by revive in v1

# git + files
def git_state(local_path) -> LocalGitState          # url, branch, dirty, unpushed  (LOCAL, via git argv; no shell)
def bring_up(node, recipe, *, allow_dirty=False, resume_id=None) -> BringUpResult
    # ONE run_script(bring_up.sh): clone-or-fetch+checkout per repo (refuses a dirty remote tree), mkdir, receive the push tarball, start tmux session, decorate. Returns per-repo commit + whether it attached to an existing session (has-session first: D10/§13).
def push_files(node, recipe) -> list[str]           # tar over stdin, mode 0600, returns shipped relative paths
def provision(node, user_scope) -> ProvisionReport  # §8; tokens go over stdin, never argv
def pull(node, sid, remote_dirs, since_epoch) -> PullResult   # pull.sh: tar --newer-mtime, streamed to stdout, extracted locally
def sample(node) -> LoadSample                       # sample.sh: JSON on stdout
```

Laws inside this module:
- Every remote command string is built with `shlex.join` from a list; no f-string shell.
- Every call is bounded (`timeout_s` mandatory; defaults: probe 10 s, script 120 s, bring-up 600 s).
- Secrets travel on **stdin** (`input_bytes`), never in argv, never in logs; `RemoteError.command` is the argv with any `input_bytes` elided.
- `BatchMode=yes` everywhere except the interactive attach window (which is not this module).
- One ssh connection per logical step; steps that must be atomic are one script. Windows OpenSSH has no `ControlMaster`; sshd throttles unauthenticated connections (`MaxStartups`), so `launch` **serializes bring-ups per node** (a per-node `threading.Lock` in `_dispatch_node_project`).

## 7. Bring-up sequence (`--go`, menu, `magent up`)

Runs on this PC as the node user. Numbered steps are what `bring_up.sh` does on the node in one connection; lettered steps are local.

- a. `nodes.resolve(config, proj)` → `Node` (auto: §11 placement). Refuse if `user == "root"` and not explicit.
- b. `remote_mux.git_state(proj.path)` per repo (project dir is a repo, or a workspace whose immediate children are repos). Refuse: not a repo; dirty (D7); unpushed commits; detached HEAD. Message names the fix (`git push -u origin <branch>` / `--allow-dirty`).
- c. `nodes.recipe_for(proj, node, git_states)` — repos, remote dirs, push set (§8), user-scope set.
- d. `remote_mux.provision(node, recipe.user_scope)` (§8; cheap when unchanged — content-hashed, skipped if the node's stored hash matches).
- e. `remote_mux.bring_up(node, recipe, resume_id=…)`:
  1. `has-session -t <sid>` → if alive, **attach only** (another PC or a previous run owns it; §13).
  2. per repo: `git clone <url> <dir>` if absent, else `git fetch && git status --porcelain` — non-empty → refuse (`node tree dirty; recall or fix on the node`), else `git checkout <branch> && git pull --ff-only`.
  3. receive the push tarball (stdin after a sentinel), extract into the workspace root, `chmod 0600` files.
  4. `tmux -L magent new-session -d -s <sid> -c <dir> -e LANG=C.UTF-8 -- <argv>` where argv = `build_start_command(tool, base_cmd, project_dir=None)` or `claude --resume <resume_id>` when given. **No environment from this PC is forwarded**: the session gets the node user's own login environment plus `LANG`; the node user's own Claude login is what runs (D5).
  5. `decorate` (status-left ` magent @second `, F1 detach, F2 = display-message hint since VS Code is local).
  6. print JSON `{sid, attached_existing, commits: {repo: sha}}`.
- f. `spawn_attach_window(node.target, sid, mux="tmux", remote=remote_attach_command(sid, "tmux"))` → wt window titled `magent:<sid>`; supervisor redials on 255, probes `tmux -L magent has-session -t <sid>` on any other exit.
- g. `launch.ensure_node_sync()` (§10) if not alive. Placement recorded in `~/.magent/nodes/node-map.json` (`{project_name: {nick, sid, placed_ts}}`).
- h. Launch row shows `[@second]` (routed-style badge, node projects only). Tiling: unchanged (attach windows are already tiled by title).

`down <name>` / `down --all` for node sessions: `pull` once more → `kill_session` → map entry cleared. The daemon is stopped by `down --all` only.

## 8. What crosses the wire (D3, D5)

**Push set** (per bring-up; computed locally by `nodes.push_set(proj, repos)`):
1. In each repo: `git ls-files --others --ignored --exclude-standard -z`, filtered by patterns `**/.env`, `**/.env.*`, `.claude/settings.local.json`, `CLAUDE.local.md`, `.mcp.json`. Never anything `git ls-files` (tracked) already covers.
2. `proj.push` entries (relative to the project dir; missing → warning, not error).
3. The project's auto-memory: `~/.claude/projects/<encoded(local project dir)>/memory/` → node `~/.claude/projects/<encoded(remote dir)>/memory/`.

`magent node plan <project>` prints this list; `magent node push <project>` re-ships it to a live node session (`.env` edited after bring-up).

**User scope** (`nodes.user_scope()` gathers locally; `provision.sh` applies on the node as the user; each item content-hashed, skipped when unchanged):

| Item | Source on this PC | On the node |
|---|---|---|
| gh token | `gh auth token` (subprocess, stdout captured, never logged) | `gh auth login --with-token` from stdin; skipped if `gh auth status` already OK with the same login |
| Claude settings | `~/.claude/settings.json` | merged as-is except `hooks`: each hook whose command's first token is not `command -v`-resolvable on the node is dropped and reported |
| MCP servers (user scope) | `~/.claude.json` → `mcpServers` | merged into the node's `~/.claude.json` by key (node wins nothing; PC is truth) via `python3 -c` on the node |
| MCP OAuth entries | `~/.claude/.credentials.json` → only keys that are MCP OAuth entries (never the Claude OAuth block) | merged into the node's `.credentials.json`, 0600 |
| plugins | `claude plugin list --json` (or the settings `enabledPlugins` map, whichever the installed CLI exposes — implementer verifies) | `claude plugin install <name>` for each missing |
| skills | `~/.claude/skills/` | tar → `~/.claude/skills/` |
| state hook | packaged `state_hook.sh` | installed to `~/.magent/bin/state-hook.sh`; wired into the node's `settings.json` hooks with the same event map as `magent hooks install` (`UserPromptSubmit`→working, `PostToolUse`→working (throttled), `Stop`→done-if-ledger-drained, `Notification`→needs-input, `SessionStart`→idle, `SessionEnd`→clear) |

Not transferred, ever: `~/.ssh`, the Claude OAuth block, `ANTHROPIC_API_KEY`, anything under `~/.claude-swap-backup`.

## 9. Status bar

`psmux.status_brand(nick)`:

```
nick None  -> "#[bold,fg=green] magent #[default]",           len " magent "      = 8
nick "second" -> "#[bold,fg=green] magent #[default]@second ", len " magent @second " = 8 + 1 + 6 + 1 = 16
```

`status-left-length` = that length + 2. ASCII-only law holds (`@` and `[a-z0-9-]` only); the 6-char cap on nicks is what keeps the F1/F2 hints on the right from being clipped at 80 columns (8 + 16 + 35 < 80 with room for `#W`). The local psmux path calls it with `None` — zero visible change today, pinned by the existing status-line tests.

## 10. The daemon — `magent node sync -d`

`node_sync.run_sync_loop(config, *, once=False)`; detached like `attention -d` (`spawn_detached`, pid file `~/.magent/node-sync.pid`, heartbeat `~/.magent/node-sync.heartbeat`, `lockfile.exclusive_lock("node-sync")`).

Per tick (every `min(pull_interval_s, sample_interval_s)`), **one ssh per node**, `pull.sh` doing all of:
- `tmux -L magent ls -F '#S'` → `~/.magent/nodes/<nick>/sessions.json` `{ts, sessions:[…]}` (node liveness for `status`/picker, age-bounded by `2 × pull_interval_s`; older ⇒ shown as `stale`, never as dead).
- `sample.sh` → append `{ts, nproc, load1, load5, load15, mem_total_mb, mem_avail_mb, my_sessions}` to `~/.magent/nodes/<nick>/load.jsonl`; truncate to `history_h`.
- for each live sid this PC owns (node-map): `tar --newer-mtime=@<since>` of `~/.claude/projects/<encoded remote dir>/` and `~/.magent/state/` → extracted under `~/.magent/nodes/<nick>/<sid>/{transcripts,state}/`; `since` stored in `pull.json`. Whole changed files, not deltas (no rsync on this PC; measured, tune later).

Failure policy: a node that is unreachable is logged once per state change (not per tick), its snapshot is left in place with its old `ts`, and the loop continues with the others. `RemoteError` never kills the loop; anything else is logged at exception level and re-raised (Sentry), like `run_server`.

Supervision: `upload_server._supervise_node_sync` → `launch.ensure_node_sync()` (probe pid + heartbeat age; respawn bounded by the same cooldown pattern as the upload watchdog). Gates: `MAGENT_NODE_SYNC=0` and "no project has `node`". **Test-isolation law:** every fixture that starts a real `serve`/`attention -d` sets `MAGENT_NODE_SYNC=0` alongside the three existing opt-outs; `tests/conftest.py` pins it for every tier.

## 11. Placement for `node: "auto"` (`nodes.place`)

Pure function over the samples on disk; the daemon owns sampling, `place` never talks to a node.

```
window   = samples from the last 30 min for each node
if any node has < 5 samples in window: one live `remote_mux.sample()` for that node, used as its only sample
u(s)     = s.load1 / s.nproc
score    = p75(u) + 0.5 * max(0, max(u) - 1.5 * p75(u))        # spike penalty: someone's bursty session
         + 0.5 * max(0, 0.15 - mem_avail_mb / mem_total_mb)   # memory pressure below 15 % free
         + 0.05 * my_sessions                                  # spread my own fleet
floor    = a node whose latest sample has mem_avail_mb / mem_total_mb < 0.10 is INELIGIBLE
           (a box at 5 % free memory OOM-kills the session; the soft term above caps at 0.075
           and cannot express that) — unless every node is below the floor, then the score decides
pick     = lowest score among eligible; ties by config order
```

Sticky: an existing `node-map.json` entry wins unless its node is gone from config (then re-placed with a printed reason). `magent node plan` prints the table (nick, samples, p75, spike, mem, my sessions, score, chosen) without writing. Assignment never written into config (same law as `account-map.json`).

## 12. Recall — `magent node recall <project> [--to <nick> | --local]`

1. `pull` once more (best effort; unreachable node ⇒ proceed with what was pulled, say so).
2. Report the last known commit per repo and whether the node tree was dirty at last contact.
3. `kill_session` (best effort).
4. Install transcripts + memory into the destination's `~/.claude/projects/<encoded(dest dir)>/` (`--local`: this PC's encoded local path; `--to`: the target node's encoded remote dir, pushed inside the next bring-up).
5. Clear the map entry; `--to` then runs the normal bring-up with `resume_id = latest transcript stem`. `--local` prints `cd "<dir>"` and `claude --resume <id>` as two separate lines (after `git pull`) rather than launching — the user chooses the terminal. The two lines are never joined by `&&`, which Windows PowerShell 5.1 cannot parse; when `<dir>` is on another drive than the shell's (or the shell's own folder is gone), a `(cmd.exe: use cd /d)` line follows the `cd`, since cmd's plain `cd` does not switch drives.

**Encoded-dir rule** (`nodes.encoded_project_dir(path)`, delegating to the ONE encoder in `sessions/claude.py`): read from the Claude Code binary and confirmed against 295/297 real store entries — every UTF-16 unit not in `[A-Za-z0-9]` → `-` (so `_`, `.`, space, `&` all become `-`; an emoji becomes `--`; the drive letter keeps its case); a result longer than 200 units is cut to 200 and gets `-` + base36(|Java hashCode(original path)|) appended. Pinned by unit vectors plus a test that reads *this machine's real* `~/.claude/projects/` entry for the repo (skipped if absent) — the rule is Anthropic's, and the test is what makes recall trustworthy. (main's encoder kept `.`/`_` — a live bug that silently dropped `--continue` for such paths; fixed in PR-B's first, cherry-pickable commit.) `resume_id` = stem of the newest top-level `<uuid>.jsonl` (the `sessionId` field equals the stem); `agent-*.jsonl` and everything under `<uuid>/` are excluded. If `claude --resume` rejects a transcript whose records carry a foreign `cwd`, recall degrades to "transcripts are on disk at <path>; resume by hand" — a message, never a crash. (Implementer verifies on this PC before shipping and records the finding in DESIGN.md.)

**Node reboot / dead session** while still placed: the attach supervisor gives up after `SESSION_MISSING_MAX`; `magent up` (and menu revive) recreates the session with `resume_id` from the pulled transcripts — the one place a remote project gets an explicit resume.

## 13. Concurrency and multi-PC

- Two PCs (desktop, laptop) with the same config may bring up the same project on the same node: `bring_up.sh` does `has-session` first and **attaches instead of creating**; the second PC's map records it as `attached_existing`. The daemon on each PC pulls independently (idempotent).
- Bring-ups are serialized per node on one PC (§6) and the daemon takes the same per-node lock so a tick never races a bring-up's `git` on the same repo.
- Sessions are named by sid, so two projects with the same title collide exactly as they do locally today (same `session_name` rule; not a new problem).

## 14. `magent node setup <nick> [--user U]... [--key PUBFILE]` and `node doctor`

`setup` (idempotent; every step prints `ok`/`did`/`skip`; needs the root hop once):
1. root: `apt-get install -y tmux git curl python3`; `useradd -m -s /bin/bash U` for each user (skip if exists); write `--key` (default: this PC's `~/.ssh/id_ed25519.pub`, or `id_rsa.pub`) to `authorized_keys` (0600); `usermod -aG docker` if the group exists.
2. as U: install Claude Code with the native per-user installer (`~/.local/bin`, auto-updates per user, no root); `gh` via the official apt repo (root step) if missing; generate `~/.ssh/id_ed25519` if missing (comment `magent@<host>`); print the pubkey; `gh ssh-key add` it from this PC (needs the gh token provisioned first — setup runs provision (§8) before this).
3. `provision` (§8).
4. `doctor`.

`doctor <nick>` (WARN-at-worst in `magent doctor`, exit codes in `node doctor`): tmux/git/claude/gh on PATH; `claude auth status` (manual login is the one thing setup cannot do — prints `ssh <target> claude` as the fix); `ssh -T git@github.com` as the user (key registered?); `LANG` sane; free disk under `root`; daemon heartbeat fresh; snapshot age.

## 15. `magent node` CLI surface

```
magent node                      # table: nick host user  load(p75 30m)  mem  my sessions  daemon
magent node setup <nick> [--user U]... [--key F]
magent node doctor [<nick>]
magent node plan <project|--all>  # placement table + push set; writes nothing
magent node push <project>        # re-ship push set
magent node recall <project> (--to <nick> | --local)
magent node sync -d [--once]
```

`status`, `sessions --json`, `up --json` gain `node` (nick or null). `status` exit 3 gains "node-sync stale". `doctor` gains `nodes`.

## 16. Testing

Unit (all OSes, fake `ssh`/`gh`/`git` on PATH recording argv — same device as `_fake_ccswap.py`; **never** the real binaries for anything that could write):
- `test_config_nodes.py`: parse, every validation rule, migration 4→5, example/docs drift pins.
- `test_nodes.py`: `recipe_for`, `push_set` against a temp git repo with ignored files, `encoded_project_dir` (incl. the real-layout pin), `place` over committed load fixtures (`tests/fixtures/node_load/*.jsonl`: quiet box, bursty box, memory-starved box, too-few-samples), stickiness.
- `test_remote_mux.py`: argv shapes (`shlex.join`, `BatchMode`, timeouts), secrets-on-stdin (a test that greps every recorded argv for the fake token), `RemoteError` redaction, `has_session` tri-state.
- `test_attach_client.py`: `--mux tmux` command + probe + marker; `--mux` default keeps every existing assertion byte-identical (characterization first).
- `test_attach.py`: `spawn_attach_window` lift is behaviour-preserving (pin before lift).
- `test_launch_nodes.py`: `--go` with a node project → recipe built, `bring_up` called once, attach window spawned, psmux untouched; `--dry-run` → zero subprocesses; dirty tree → refusal text; serialization per node.
- `test_status_line.py`: `status_brand(None)` byte-identical to today; `status_brand("second")` string + length; 6-char cap.
- `test_node_sync.py`: one ssh per node per tick; unreachable node isolation; snapshot/`pull.json` layout; heartbeat.
- `test_node_provision.py`: settings-hook filtering, MCP merge (never the Claude OAuth block), hash-skip.
- Shell scripts: `shellcheck` in the gate (`scripts/check.py` runs it if installed; CI installs it) + each script's argument contract exercised by the e2e tier.

e2e (`needs_ssh`, CI-only, the existing loopback sshd fixture, no new provisioning): the runner is its own "node" (`user` = runner user, `root` = tmp): real `git clone` from a local bare repo, real `tmux -L magent` session hosting `_fleet_agent.py`, `magent-attach-client --mux tmux` redialling through a killed connection, a real `pull` producing a transcript-shaped file, `node doctor` green, `down` killing the session. Windows leg: the client side only (wt window + supervisor), the "node" being the loopback sshd's Linux-shaped shell is out of scope — `tmux` is not on a Windows runner, so the Windows leg pins the argv contract via the fake and the reconnect via the existing tier.

## 17. Fast-ship plan

1. PR-A (this spec) → PR-B config+nodes+remote_mux (pure + fakes) → PR-C attach `--mux` + spawn lift → PR-D launch bring-up + status bar + `up`/`down`/`status` → PR-E daemon + supervisor + attention read → PR-F setup/provision/doctor → PR-G recall + placement → PR-H docs/CHANGELOG/example/release. Each PR: full gate on box-fifth (bundle back; hooks honoured), squash into `feat/nodes`.
2. When `feat/nodes` gate is green end-to-end on the developer's real pool (box-second, user `alice`): merge to main, tag `3.20.0`, watch CI **after**, `3.20.1` for what it finds.
3. Ledger (DESIGN.md §3): D12 items; "transcript pulls ship whole files"; "Windows leg of the node e2e is argv-only".

## §18 The cloud backend (`"node": "cloud"`)

1. **Built-in nick.** `node: "cloud"` needs no pool entry. A pool entry named `cloud` is a ConfigError, and so is `cloud` + `host` [V23]. A cloud project runs tool `claude` and carries `cloudTask`, the task text the session starts on. `cloudTask` is OPTIONAL at config load, because the nodes release already parses `node: "cloud"` and a required field would break configs users already have; a malformed value is a ConfigError, and a missing one is refused at create time with a named reason (J7). The text must never look like a session id or URL, because `--cloud <id|url>` attaches instead of creating [V1]. magent never creates a session with `-p "<description>" --cloud`: the documented create path is the interactive form in a pane [V24].
2. **The environment is chosen outside magent.** `--environment` takes only self-hosted `ccpool_` ids [V2]. Self-hosted environments are a Team/Enterprise beta [V43]. The hosted environment is the user's `/remote-env` pick or `remote.defaultEnvironmentId` [V14][V39]. Environments are created and edited only in the claude.ai/code dialog [V14]. There is no `cloudEnvironment` field.
3. **D7, verbatim.** Git is the truth. The VM clones the GitHub remote at the checkout's branch [V7], and work comes home by `git pull` + teleport [V12]. A create is refused on: no remote or a non-GitHub remote [V8]; a detached HEAD; uncommitted or unpushed work, which the clone would not contain [V7]. Untracked files never reach the session on either path [V37], so the launch preview and the hand-off text say so. magent never sets `CCR_FORCE_BUNDLE` and never passes `--allow-dirty`.
4. **Pin-only.** `auto` never places onto cloud: `cloud` is not a pool member [V23].
5. **The pane.** It is a LOCAL psmux session running `cmd /c claude --cloud "<cloudTask>"` [V19], branded through D's `status_left("cloud")` (" magent @cloud ", 15 cells, status-left-length 17) [V20]. Every `--cloud` makes a NEW session [V10], so the pane is typed exactly once: no re-send, no revive [V19], and the idle reaper never parks it. The create gate runs only when no psmux session exists yet. A cloud row's liveness is the LOCAL pane's: the VM pauses after a few minutes idle and is later reclaimed [V17], so no magent text calls a cloud row "dead" because the VM is idle.
6. **Still works:** `attach`; F2, which opens the LOCAL checkout; `peek`; `down`, which kills only the local pane (the cloud session persists [V11], and `down` says so); rows in `status`, `up --json` and `sessions --json`, which carry `"node": "cloud"`.
7. **Refused, with a reason.**
   - **Alt+V and the phone page:** the image would land on the PC. `serve` answers 409 and Alt+V narrates `cloud-pane` [V21].
   - **`send`/`model`:** they would type into a viewer. `send` names `claude -p "<msg>" --cloud <session-id>` [V5].
   - **`attach --no-mux`:** it would create a second session.
8. **Not applicable:** provision, ssh shipping, sync/pull, placement, node-map, tmux `--mux`, and D's Remote-SSH F2. A cloud session's MCP config is never written by magent.
9. **Recall.** `magent node recall <p> --local` prints `cd`, `git pull` and `claude --teleport <id>` when an id is visible in the pane, else the bare picker [V13]. Teleport needs the same repo, the branch pushed and the same account [V12]; with a dirty tree it offers a stash, so recall never hard-blocks on a dirty checkout. The session id may be a `session_…` id, a `cse_…` id or a `claude.ai/code/session_…` URL [V1][V40]. The copy is independent, and `--to <nick>` is refused.
10. **What can carry a secret or file into a cloud session (DECISION-8).** No CLI or API writes any of these [V4][V14]:
    - **Environment variables** are dialog-only `.env` text, copied once at start into ordinary process variables that any command can read. "Anyone who uses the environment can read the values," and a shared environment must hold no secrets [V14][V25][V26].
    - **API credentials** are Pro/Max only and need the admin role. The agent proxy adds a header for the listed hosts, and the key "never reaches Claude, the commands it runs, or the session's environment variables" [V15][V28].
    - **The setup script** is dialog-only. It runs as root before launch and is cached as a filesystem snapshot [V27]. It never gets API credentials [V28].
    - **Committed `.claude/settings.json` `env` or `.mcp.json`** is readable by anyone who reads the repo, so it is never used for secrets [V29].
    - **A SessionStart hook** runs after launch, on start and on resume [V32]. Two routes carry it: a hook committed in the repo's own `.claude/settings.json` (the primary route; single-repo sessions only [V30]) and a plugin synced from claude.ai (secondary; needs Claude Code 2.1.287 or newer and a claude.ai sign-in rather than a token [V30][V31][A: U7]). A repo's `enabledPlugins` does NOT install a plugin in cloud [V29], so it is never the route.
    - **`claude --cloud` itself** uploads nothing on the clone path. A bundle carries tracked changes but never untracked files, and on native Windows a tracked `.env` IS bundled (credential-named files are left out only on macOS, Linux and WSL) [V7][V37]. Rule 3 makes the clone the only path.
11. **Decision: three tiers, safest first (DECISION-14, refined by DECISION-18).**
    - **a. API credential.** For a key used only as an HTTP header to a known host (Pro/Max), the value never enters the VM. magent names the candidates; the user adds them.
    - **b. Sealed (phase 2; cut until J0-B passes).** `magent node push` tars the push-set files inside the project and encrypts them with `age` to a recipient made by `magent node cloud-setup`. It pushes the ciphertext as a lone commit from the PC (never through the cloud proxy, which rejects non-branch pushes [V36]) to a carrier in the project's own GitHub repo: the custom ref `refs/magent/sealed`, or the fallback branch `magent-sealed`; no working tree is touched [A: U8, fetch half]. A SessionStart hook (the repo-committed `.claude/settings.json` route first, a synced plugin second) fetches it through the GitHub proxy. It decrypts with `MAGENT_UNSEAL`, the age identity and the ONE secret, pasted once into a personal environment, and delivers the values as `export` lines in `CLAUDE_ENV_FILE`, idempotently on every start and resume [V32][V33]. It extracts only paths the checkout gitignores. `age` is used because the stdlib has no authenticated cipher, and magent writes no crypto of its own. The VM gets `age` from one setup-script line, which must be idempotent because the setup script can re-run on a cache rebuild or a rebuilt VM [V27]; archive.ubuntu.com is on Trusted [V35]. If J0-B finds no hook route working or no carrier fetchable, (b) is cut until J0-B passes [DECISION-18].
    - **c. Manual (phase 1; built first, and the only tier if (b) is cut).** Used when there is no key, no `age`, or a public or unknown-visibility repo. Names print masked; the full `KEY=value` block goes to a 0600 temp file for pasting into the dialog. Files other than `.env*` cannot travel. Values pasted into an environment are re-read each time Claude Code starts in the VM [V25], so an edit reaches an existing session after a pause or rebuild, though not immediately.
    - **The create gate** passes once the current push set is sealed or handed off. The check is a keyed HMAC digest, never a value.
12. **Threat model.**
    - Anthropic can read the key and the plaintext in every tier except (a).
    - Sealed keeps values out of the environment config and out of the dialog. It rotates with one re-paste, via `cloud-setup --rotate`.
    - In (b) and (c), a prompt-injected agent in the VM can read both the plaintext and the key [V25][V34]. The hook may append `unset MAGENT_UNSEAL` to `CLAUDE_ENV_FILE`, as defence in depth only: the docs describe only `export` lines [V33], and whether `unset` takes effect is U11.
    - A leaked key opens every sealed set of that user until rotation.
    - A public repo means public ciphertext, so sealing is refused there.
    - **Rejected:** decrypting in the setup script (the snapshot would keep the plaintext [V27]); a gist (not on Trusted, and the proxy's API scope is attached repos [V36]); pulling from the PC over the network (the PC must be online, and it would expose the PC).
13. **Durability, phone and Session 0.** The cloud session outlives the pane, the PC and the laptop [V11]. Its VM pauses after a few minutes without activity (files kept) and is later reclaimed (a fresh VM, history restored, background work lost) [V17]. The Claude app and claude.ai/code show and steer it [V11]. `/login` and `/web-setup` need a desktop terminal, because a browser from a Session-0 pane is invisible [V22]. `--cloud` needs a claude.ai login: a `claude setup-token` credential is inference-only and does NOT authorize it, and a token-authenticated session does not sync plugins [V18]. It also needs the `allow_remote_sessions` policy, and is unavailable on Bedrock, Vertex and third-party providers [V18]. A follow-up to an existing session is `claude -p "<msg>" --cloud <id> --output-format json`, never `stream-json` [V5].
14. **Impossible from the CLI.** The CLI cannot list, stop, archive or delete a cloud session [V4][V11]. It cannot create or edit an environment, variable, credential, setup script, network level or plugin upload [V14][A]. magent never pretends otherwise.
15. **Network.** Network access is set per ENVIRONMENT in its dialog (shared environments on the admin page), never per session [V35]. At every level, GitHub (through its proxy) and the Anthropic API stay reachable [V44], so the sealed fetch (item 11b) needs no network change. `age` installs from archive.ubuntu.com, which the default Trusted level allows [V35].
16. **MCP in cloud.** A cloud session's MCP servers come from the repo's `.mcp.json` (one repo), from synced plugins, and from claude.ai connectors. They never come from `~/.claude.json` or the environment config [V29][V31]. magent writes none of them: the phase-2 magent-cloud plugin carries no `.mcp.json` (item 8). `"magent-cloud@synced": false` in the user `enabledPlugins` keeps the plugin out of the PC's and nodes' own sessions [V31].

## Facts table (re-verified 2026-10-02 against Claude Code 2.1.284)

First written 2026-09-24 against 2.1.280. Re-checked 2026-10-02 against the code.claude.com docs fetched that day and `claude --version`, `claude --help`, `claude setup-token --help` and `claude auth --help` from 2.1.284. Nothing that creates, lists or attaches to a session was run. Code-fact rows (V19-V23, V45) are re-anchored to main. "Phase 2" notes mark rows whose consequence lands in J4, J5, J11s or J12s, which wait on the user-run J0-B probe.

| # | Fact | Source |
|---|---|---|
| V1 | `--cloud [description\|session_id\|url]`: "Create a cloud session with the given description, or attach to an existing one by session ID or claude.ai/code URL". An id is `session_...` or `cse_...`; with an id the form is `-p "<msg>" --cloud <id>` and queues a message. Never rely on an interactive attach. Bare `--cloud` with no value is treated as absent; `--remote` is a deprecated alias | `claude --help` (2.1.284), CLI reference |
| V2 | `--environment <environment_id>`: "self-hosted environment (ccpool_...)". Anthropic-hosted `env_` ids are rejected; use `/remote-env`. `--ref <branch>` exists only with `--environment` | `claude --help`, cloud-environments "Select an environment" |
| V3 | `--teleport [session]`: "Resume a teleport session, optionally specify session ID" | `claude --help` |
| V4 | `agents`/`attach`/`stop`/`logs`/`rm`/`respawn` manage LOCAL and background sessions. `agents --json` prints active local sessions. No subcommand lists or stops cloud sessions, so magent keeps its own record of each id | `claude <sub> --help`, agent-view docs |
| V5 | `claude -p "msg" --cloud <id>` queues and exits. `--output-format json` gives `{ok, session_id, url}` on success or `{ok: false, session_id, error}` on failure; `stream-json` is not supported with an id. Map the failure, "archived" and org-policy errors to specific messages. Use this form only for follow-ups to an existing id | claude-code-on-the-web, "Send follow-ups from the CLI" |
| V6 | `--cloud <id>` without `-p` gives "Attaching to an existing cloud session is not enabled for your account." | same doc, error table |
| V7 | The VM clones the repo. With no remote or no GitHub App, the local repo is bundled, including uncommitted tracked changes, and the bundle holds the full history of all branches. "`--cloud` works with a single repository at a time." Untracked files are never included | same doc, "From terminal to cloud" |
| V8 | A non-GitHub repo (via `CCR_FORCE_BUNDLE`) "can't push results back" | same doc, Limitations |
| V9 | A live provisioning checklist is shown; typed messages are queued until ready | same doc |
| V10 | "each `--cloud` command creates its own cloud session" | same doc, "Run tasks in parallel" |
| V11 | Cloud sessions persist when the laptop closes; they can be monitored from the Claude app; the claude.ai/code sidebar can archive or delete | same doc |
| V12 | Teleport requires the same repo (not a fork), the branch pushed and the same account. A dirty tree gets a stash prompt, not a refusal, so recall never hard-blocks on it. The terminal gets its own copy | same doc, "Teleport requirements" |
| V13 | `claude --teleport` with no id opens a picker. `/teleport` inside the session prints `claude --teleport <id>` (session v2.1.223+) | same doc, "From cloud to terminal" |
| V14 | Environment variables are set in the environment dialog, in `.env` format. "Anyone who uses the environment can read the values." Environments you create are personal. `/remote-env` "can't add or edit environments" | cloud-environments |
| V15 | API credentials exist on Pro/Max only (not Team or Enterprise). The proxy adds them; the key "never reaches Claude, the commands it runs, or the session's environment variables" | cloud-environments |
| V16 | Four network levels: None, Trusted (the default allowlist), Full, Custom | cloud-environments |
| V17 | Two idle stages. After a few minutes without activity the VM pauses with its files saved, and the next message restores it; a paused VM can later be reclaimed, and reopening then restores the conversation but not running background work. A paused or reclaimed session is not dead, so no magent surface calls a cloud row dead | cloud-environments; web doc |
| V18 | A claude.ai sign-in is required; the org policy `allow_remote_sessions` must be on; there is no separate VM charge, and rate limits are shared with all other usage. Not available on Bedrock, Vertex or third-party providers | web doc |
| V19 | psmux types `cmd /c <command>` (`platform/windows.py::_send_argv`, L451, the f-string at L465). `_verify_sends_landed` (L1072) re-types. `psmux.revive_sessions` (L2087) re-types (`cmd /c` at about L2190 and L2208) | code, main (bf5b21ba) |
| V20 | `_STATUS_BRAND = "#[bold,fg=green] magent #[default]"` is 8 cells (`_STATUS_BRAND_CELLS`, `psmux.py` L1296); `_STATUS_LEFT_HEADROOM` is 2; `status_left(nick)` adds `len("@<nick> ")` | code, main (`psmux.py` L1288-1297, L1398-1405) |
| V21 | The upload server validates the project before writing (`upload_server.py::_handle_post`, L1501, the `Unknown project` 400 at L1574). The Alt+V outcomes are a closed, pinned vocabulary (`altv.ALTV_OUTCOMES`, L76; `test_altv.py::test_every_outcome_name_is_declared_and_reasoned`, L336) | code, main |
| V22 | A browser launched from a Session-0 psmux pane is invisible; `/login` breaks there (Windows OpenSSH is a service). Run `/login` and `/web-setup` from a desktop terminal | observed live; a local note, not in this repo, so no test depends on it |
| V23 | `cloud` is a reserved nick (`config.NODE_CLOUD`, `_RESERVED_NICKS`): no pool entry, `cloud` + `host` is an error, `nodes.resolve` refuses it | code, main (`config.py` L661-676, `nodes.py::resolve`) |
| V24 | A non-TTY `--cloud` is refused. The docs are silent and the CLI was not probed, so this stays "not re-run". magent runs the interactive form in a pty pane, and never creates a session with `-p "<description>" --cloud` (`-p` is documented only for follow-ups to an id) | live probe 2026-09-19 (earlier session; not re-run) |
| V25 | A session reads the environment's values when it is created and again each time Claude Code starts in its VM afterward. An edit reaches an existing session after a pause or reclaim, not immediately | cloud-environments |
| V26 | Shared environments: "don't include secrets" | cloud-environments |
| V27 | The setup script runs as root on Ubuntu 24.04 before launch; it must exit 0 (about five minutes); its result is cached as a snapshot of files, not processes. It does not run when a VM is restored after idling, but a rebuilt VM can re-run it (script or host change, or roughly seven days of cache expiry), so a setup script must be idempotent | cloud-environments |
| V28 | API credentials need the admin role and an existing hosted environment; requests leave Anthropic's network; the types are Bearer, allowed websites (`*.` allowed) and custom headers; the agent proxy connects "when it launches, after the setup script has run" | cloud-environments |
| V29 | The carry-over table: repo files (`CLAUDE.md`, `.claude/settings.json`, `.mcp.json`) travel; `~/.claude.json` and user settings do not; skills enabled on claude.ai load; a repo's `enabledPlugins` do NOT install in cloud. Phase 2: carry the unseal hook as a committed repo hook or a claude.ai-synced plugin, never `enabledPlugins` | cloud-environments |
| V30 | Which hooks run in cloud is contested: the cloud-environments page says repo and server-managed hooks only, the hooks page also lists synced plugins, and the changelog (2.1.287) fixed "SessionStart hooks from synced plugins not running in new cloud sessions". Phase 2: stays the mandatory live probe U7 (J0-B), run on 2.1.287 or later | cloud-environments; hooks docs; changelog |
| V31 | Synced plugins load their hooks and MCP servers where you sign in with your claude.ai account; no sync happens when `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_AUTH_TOKEN` supplies the credential. `"<name>@synced": false` in user `enabledPlugins` disables one locally. Phase 2: a synced plugin is never the only unseal path | plugins docs; settings reference |
| V32 | The setup script runs before Claude Code launches; SessionStart hooks run after, on every start and resume; a command hook times out at 600 s unless it sets `timeout` | cloud-environments; hooks docs |
| V33 | `CLAUDE_CODE_REMOTE=true` in cloud sessions. A SessionStart hook may write `export` lines to `CLAUDE_ENV_FILE`, applied to later Bash commands (not the model context). Only `export` is documented, so nothing depends on `unset` | hooks docs; cloud-environments |
| V34 | A hook inherits Claude Code's environment minus `OTEL_*` and scrubbed variables; `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` strips credentials it recognises from Bash, hooks and stdio MCP, so no variable magent adds is named like a credential | hooks docs; env-vars docs |
| V35 | Network access is set per environment (there is no organization-level allowlist); Trusted lists package registries including archive.ubuntu.com, and no `*.ts.net`. The default list was not diffed on 2026-10-02 | cloud-environments |
| V36 | GitHub goes through a proxy: cloning, fetching and PRs work normally. The proxy rejects non-branch pushes and branch deletes; any branch may be updated. API and release assets reach attached repos only. Phase 2: a sealed carrier is pushed from the PC, never from the VM, and a custom-ref fetch through the proxy is unproven (U8) | cloud-environments |
| V37 | A bundle never includes untracked files. Credential-named files (`.env`, `*.tfvars`, `id_rsa`, `*.pem`) are left out of a bundle on macOS, Linux and WSL only, so on native Windows a tracked `.env` IS bundled | web doc |
| V38 | `http`/`ws` MCP entries take `headers` (and `headersHelper`); `${VAR}` expands in `url`/`headers`, except recognised credential names, which read empty | MCP docs |
| V39 | `remote.defaultEnvironmentId` selects the default cloud environment (`env_...` hosted or `ccpool_...` self-hosted; the latter is read only from user and managed settings). magent may read it, never write user settings | settings reference |
| V40 | `CLAUDE_CODE_REMOTE_SESSION_ID` carries a `cse_` id; the claude.ai/code URL (`https://claude.ai/code/session_...`) uses `session_`. `--cloud` accepts both, so either may be stored | cloud-environments |
| V41 | Custom connectors authenticate with OAuth, and their traffic originates from Anthropic's cloud | cloud-environments; support article 11175166 |
| V42 | Context7 `/anthropics/claude-code` answered "monthly quota exceeded" on 2026-09-24; the docs above were fetched directly instead | tool output, 2026-09-24 |
| V43 | Self-hosted environments are a Team/Enterprise public beta, off by default | self-hosted-environments docs |
| V44 | Always reachable at every network level: GitHub (via its proxy), claude.ai connectors (via Anthropic), API-credential hosts, and the Anthropic API | cloud-environments |
| V45 | `cli/hooks_cmd.py` round-trips `~/.claude/settings.json`: `_default_settings_file` (L51), `_load_settings` (L79, returns `dict \| str`, where the `str` is the refusal text and nothing raises), and an inline tmp-file + `os.replace` write in `hooks_install_cmd` (L237); there is no backup and no reusable writer | code, main |
| V46 | `--cloud` needs a claude.ai login. The scope that controls cloud sessions is capped server-side at 30 days, and `claude setup-token` (one year, inference only) does NOT authorize it. A headless node therefore cannot create cloud sessions on a token, and a token-authenticated session does not sync plugins | self-hosted-environments docs; `claude setup-token --help` |
| V47 | Remote Control (`--remote-control`) and `--teleport`'s error wording share infrastructure, but Remote Control is a different feature (your own hardware) and is never conflated with `--cloud` | web doc; CLI reference |
| A | age is FiloSottile/age, installable with `winget install FiloSottile.age` (package confirmed 2026-10-02, version 1.3.2); `age-keygen` prints `# public key: age1…` and an `AGE-SECRET-KEY-1…` line | ASSUMED for the keygen output format (not probed) |
