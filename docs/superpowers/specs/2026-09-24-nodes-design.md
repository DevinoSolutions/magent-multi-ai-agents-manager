# Nodes: run a project's agent on a pool of machines, with git as the source of truth

**Status:** design approved in conversation 2026-09-24, awaiting spec review
**Branch:** `feat/nodes` (long-lived feature branch; main stays small and proven)
**Release plan:** ship `3.20.0` on a green local full gate, let CI run after, cut `3.20.1` for anything CI finds (user's explicit call for this feature; the standing "all checks green before merge" rule is suspended for this branch only)

---

## 1. Problem

This PC runs ~60 agent sessions and is out of RAM and CPU. The org has Linux boxes
(`devino-second` … `devino-fifth`, ssh already working) with spare capacity. We want a
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
| D1 | **Git is the truth.** The node holds a `git clone` of the project's repo(s) at the branch you are on. No mirror, no rsync, no two-way sync, no lock on the local folder. | Removes the whole cross-OS mirror bug class (native deps, exec bits, CRLF, OneDrive, cwRsync). Work comes home the way it already does: the agent commits and pushes. |
| D2 | **Node-only data comes home continuously, one way.** Claude transcripts + the node's `~/.magent/state/` are pulled every 30 s into `~/.magent/nodes/`. | Durability + resume-anywhere without conflicts (append-only data). |
| D3 | **Explicit + auto-detected non-git inputs are pushed at bring-up**: gitignored `.env*`, `.claude/settings.local.json`, `CLAUDE.local.md`, ignored `.mcp.json`, and the project's auto-memory dir. `push: [...]` adds extras. | A session can't work without them; keys/certs stay opt-in. |
| D4 | **Dedicated per-person Unix user on each node** (`amin`, `aladdin`), created by `magent node setup`; root is used for that one hop only. magent refuses to run sessions as `root` unless `"user": "root"` is written explicitly. | Credential isolation on shared boxes; blast radius; attribution; matches Claude Code's per-user state. |
| D5 | **User-scope auth and config are provisioned one way at every bring-up**: `gh` token, Claude `settings.json` (hooks filtered to what exists on the node), user-scope MCP servers **and their OAuth entries**, plugin list, skills dir. **The Claude login itself is manual per node** (its refresh token is single-holder; two machines refreshing it log each other out — the ccswap failure mode). | "Transfer authentication where it matters" (user), minus the one transfer that would corrupt the source. |
| D6 | **Per-node git identity**: `node setup` generates an ed25519 key on the node in the user's home (never leaves the box) and registers it to the user's GitHub account via `gh ssh-key add`. Agent forwarding is *not* relied on. | Pushes must work with no window attached, after sleep, and from a Session-0 bring-up. |
| D7 | **Refuse a dirty or unpushed local tree** at bring-up (`--allow-dirty` overrides, and then those changes simply are not on the node). | Git is truth; invisible local edits would collide with the node's pushes. |
| D8 | **Placement:** `node: "<nick>"` pins; `node: "auto"` picks by **load history**, not one sample, and sticks. | A bursty box someone else is using must not be piled onto because it was quiet for one second. |
| D9 | **Reuse the attach supervisor unchanged.** The local window is `magent-attach-client` with a new `--mux tmux`. | It took a long time to get right; one module owns reconnect. |
| D10 | **One tmux server per node user** (`tmux -L magent`), sessions named by sid; every `-t` target is the exact form `=<sid>` (bare `-t` is a prefix match on a shared socket). | Termius: `tmux -L magent attach` lands in the most recent session; `Ctrl+B s` is the picker over all of them. |
| D11 | Nothing about prod boxes in code. `root@devino` is simply not in the pool. | YAGNI (user). |
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
      "second": { "host": "devino-second", "user": "amin", "root": "~/magent" },
      "third":  { "host": "devino-third" },
      "fifth":  { "host": "devino-fifth" }
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
5. Clear the map entry; `--to` then runs the normal bring-up with `resume_id = latest transcript stem`. `--local` prints the exact `cd … && claude --resume <id>` (after `git pull`) rather than launching — the user chooses the terminal.

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

1. PR-A (this spec) → PR-B config+nodes+remote_mux (pure + fakes) → PR-C attach `--mux` + spawn lift → PR-D launch bring-up + status bar + `up`/`down`/`status` → PR-E daemon + supervisor + attention read → PR-F setup/provision/doctor → PR-G recall + placement → PR-H docs/CHANGELOG/example/release. Each PR: full gate on devino-fifth (bundle back; hooks honoured), squash into `feat/nodes`.
2. When `feat/nodes` gate is green end-to-end on the developer's real pool (devino-second, user `amin`): merge to main, tag `3.20.0`, watch CI **after**, `3.20.1` for what it finds.
3. Ledger (DESIGN.md §3): D12 items; "transcript pulls ship whole files"; "Windows leg of the node e2e is argv-only".
