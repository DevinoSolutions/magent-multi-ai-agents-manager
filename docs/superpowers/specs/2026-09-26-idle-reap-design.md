# Idle reaper: design

- Date: 2026-09-26
- Branch: `feat/idle-reap`. It stacks on `fix/state-hook-module-entry` and `fix/revive-live-agent` (see "Dependencies and stacking").
- Status: design approved. The stop mechanism is final: a hard kill only (poc-reap2). The team lead's rulings are folded in: bulk revive leaves parked sessions alone, both keystroke gates apply, v1 records stay compatible, and the isolation law is named. The user decided on 2026-09-27 that only **finished** sessions are parked: a session waiting on the user never is.
- Evidence:
  - `poc-reap.md`: fleet measurements, kill methods, pane residue.
  - `poc-reap2.md`: hard kill vs clean exit, against a real idle Claude Code.
  - `impl-revive.md` and `sp-revive.md`: the "is an agent alive in this pane" seam, its review, and the orphan-console hole.
  - `impl-hookmain.md`: the dead state hook.

Every seam named here exists on origin/main (`edac113`) or on one of the two dependency branches, unless it is marked **new**.

## Goal

Idle agents hold memory. When measured, 31 agents held 57.6 GB of commit (claude.exe alone: 30.3 GB), and the machine sat at 151.5 of 253.7 GB. Stopping the 4 sessions idle for more than 2 h would free about 9.3 GB. Stopping the 7 idle for more than 1.75 h would free about 13.3 GB.

The reaper stops the agent process tree of a local magent session that has been idle for more than X minutes (default 120):

- The **pane stays**. Its shell stays, the psmux session stays, and the window and tiling are untouched.
- The pane shows one line with the **exact command that resumes the same conversation**.

We call the result a **parked** session.

The idle reaper does not reclaim the psmux server and warm-spare overhead: about 480 MB of working set per session, and 5.3 GB of commit fleet-wide after a full reap. See "Out of scope".

## Scope

A session is in scope only when every one of these holds:

- **It runs on this machine.** It is a psmux session, so `Platform.supports_psmux()` must be true, which today means Windows. It appears in `psmux.eligible_projects(cfg)`, which keeps only local, enabled, non-IDE projects, and `psmux.live_sessions` reports it live.
- **Its agent can be read.** The project's tool resolves to an `AGENT_TOOLS` entry that has an idle probe (**new** field `AgentTool.idle_probe`). In v1 only `claude` sets one, so Codex is left out by data, not by a hard-coded name.
- **It is the only session in its directory.** No other eligible live session has the same normalized project directory (why: row R3).
- **This logon session owns its pane.** The pane's shell must belong to the logon session the reaper runs in (`procs.session_id_of` equals `procs.current_session_id`, and neither read failed).

**Remote-attach windows.** They are out of scope on the client side. The client does nothing, and the host's own `serve` treats those sessions as its own local ones.

**Excluded outright:** IDE windows, plain-command tools, sessions hosted on nodes, and any pane whose own process is not a shell.

## Terms

- **X**: the idle threshold in seconds, equal to `settings.idleReap.afterMinutes` × 60. Values under the floor of 30 minutes are raised to 30 minutes.
- **Pane shell**: the pane's own process, `#{pane_pid}`. For magent's launch shape (shape A) it is `pwsh.exe`, and the agent is typed into it as `cmd /c <cmd>`.
- **Agent root**: the one process in the pane's subtree that owns an interactive Claude Code session file (row R5). Depending on the launcher it sits at depth 1 or 2 under the pane shell.
- **Agent subtree**: the agent root plus everything below it. It comes from `procs.process_tree(agent_pid, snapshot)`, root first.
- **Session file**: `<config_dir>/sessions/<pid>.json`. Claude Code writes it and does not document it. We read it and never write it.
- **Transcripts**:
  - Main transcript: `<config_dir>/projects/<encoded cwd>/<sessionId>.jsonl`.
  - Subagent transcripts: `<config_dir>/projects/<encoded cwd>/<sessionId>/subagents/*.jsonl`.
  - The directory encoding comes from `sessions/claude.py::_projects_dir`.
- **Record**: the magent agent-state record for a cwd, `agent_state.state_for(cwd)`. It is read raw, with no attention staleness applied.
- **Sweep**: one pass of the reaper over every in-scope session.
- **Veto**: a failed row. Each veto has a named reason.
- **Park**: the act of stopping the agent and writing the `parked` record.
- **Unknown**: any reading that could not be taken, or that came back in a shape we did not expect. **Unknown is never read as idle.**

## Signals and the decision rule

A session is reaped only if **every row passes**. Rows run top to bottom, cheapest first. The first row that fails ends the evaluation, and its reason is what gets logged.

The pure part is **new** `reap.decide(signals) -> str`. It takes the readings as one frozen `reap.Signals` value and returns either `"reap"` or a veto reason from the closed set `reap.VETO_REASONS`. All I/O happens before it is called. Every row is written the way the table reads, "passes only when": a comparison with NaN is false, so a NaN reading vetoes instead of reading as old enough.

| Row | Brief | Question | Seam | Passes only when | Veto reasons |
|---|---|---|---|---|---|
| R1 | | Is reaping on at all? | **new** `reap.off_reason(cfg, plat)`: the setting, the `MAGENT_IDLE_REAP` switch, `Platform.supports_psmux()` and `Platform.logon_session_is_interactive()`, in that order | `settings.idleReap.enabled`, `MAGENT_IDLE_REAP` is not 0, and both platform probes are true | no sweep runs |
| R2 | | Is the session in scope? | `psmux.eligible_projects(cfg)`, `psmux.live_sessions`, `AGENT_TOOLS[tool].idle_probe` | local, enabled, non-IDE, live, and the tool has an idle probe | `out-of-scope` (not logged) |
| R3 | | Does it own its directory? | the eligible rows, compared with `agent_state.norm_cwd(resolved)` | no other eligible live session has the same directory | `shared-cwd` |
| R4 | | Can we see the pane's processes? | **new** `psmux.pane_trees` (one `pane_pids` fan-out and one `procs.snapshot_processes`), plus `procs.session_id_of` | the tree is known, its root is a shell (`psmux.is_idle_command(root image)`), and the root is in our logon session (both logon reads succeed and are equal: two unknown reads prove nothing) | `tree-unknown`, `pane-not-shell`, `other-logon-session` |
| R5 | | Which process is the agent? | **new** `idle_probe.sessions_by_pid(config_dir)`, which checks identity through **new** `procs.process_identity` | see "Picking the agent root" below | `no-agent`, `ambiguous-agent`, `identity-mismatch`, `cwd-mismatch` |
| R6 | 2 | Does Claude Code say it is idle? | the session file | `status` is `idle`, and `now - statusUpdatedAt/1000 > X` | `claude-busy` (any other status, `waiting` included), `claude-recent` |
| R7 | 1 | Does magent's record say the agent's turn ended? | `agent_state.read_record(session.cwd)` read raw | a record file that is there can be used (one that cannot be read, is not UTF-8 JSON, is not an object, or is nested past the parser's depth is unknown: it vetoes as `record-unreadable`, never as `no-record`); state is `done` or `idle`; `session_id` equals the file's `sessionId`; `ts` is readable (a `ts` that is missing, not a number, non-finite, or an int past any float is unknown: `None`, never 0, and it vetoes by name whatever the start time); `ts` is not earlier than the agent root's start time; and `now - ts > X` | `no-record`, `record-other-session`, `record-state`, `record-unreadable`, `record-stale`, `record-recent` |
| R8 | 3 | Has any transcript been written recently? | **new** `idle_probe.last_activity(session, config_dir)` | the main transcript exists, and the newest mtime across the main transcript and `subagents/*.jsonl` is older than X | `no-transcript`, `transcript-recent` |
| R9 | 4 | Has the user typed anything? | `psmux.capture_pane`, then `fleet.classify_state` and **new** `fleet.input_draft` | the pane state is `idle` or `limit`, and the draft is `""` | `pane-busy`, `pane-dialog`, `pane-unreadable`, `draft` |
| R10 | 5 | Is all of this still true right now? | rows R2 to R9, re-read for this one session just before the stop | every row passes again, for the same agent pid and create time | `changed` |

**Why R3 exists.** The agent-state store is keyed by cwd. When two live sessions share a directory (a multi-window project), the record cannot say which one it describes, and the resume path could not tell which conversation to resume. The veto follows from how the store is keyed. The reaper does not choose it.

**The idle probe.** **New** `sessions.IdleProbe` is a frozen dataclass with two callables, and it is the only agent-specific code the reaper calls:

- `sessions_by_pid(config_dir) -> SessionScan | None`, where **new** `sessions.SessionScan` is a NamedTuple `(sessions: Mapping[int, LiveSession], unusable: frozenset[int])`;
- `last_activity(session, config_dir) -> float | None`.

Claude's implementations are **new** `sessions/claude.py::read_session_files` and `sessions/claude.py::last_activity`. `AgentTool.idle_probe: IdleProbe | None = None` is set only on the `claude` entry.

**Picking the agent root (R5).**

1. Read the session directory once per sweep. The scan's `sessions` maps each pid to a `sessions.LiveSession` (**new** NamedTuple: `pid, created, image, session_id, cwd, status, status_ts, kind, quiet`), and holds live sessions only (see "The stale-file rule" below). Its `unusable` holds the pids whose files are there but unusable (see "An unusable file" below). If the directory cannot be read, the result is `None`, and `None` means unknown for every session.
2. If any process in the pane's tree is in `unusable`, the result is `ambiguous-agent`, checked first. That process is an agent nobody can read: unknown, not absent. It may be a second agent under the first, and the stop would kill it with the subtree.
3. Among the processes in the pane's tree, keep those that have a live session whose `kind` is `interactive`. There must be **exactly one**:
   - zero is `no-agent`;
   - more than one is `ambiguous-agent`.
4. That session's `image` must be one of the tool's `images` or in `AGENT_RUNTIME_IMAGES`, or the result is `identity-mismatch`. On this machine the agent root is `claude.exe`.
5. `norm_cwd(session.cwd)` must equal `norm_cwd(resolved)`, or the result is `cwd-mismatch`. This is what lets the resume path find the parked record by the project's directory.

**The session file contract.**

- **Fields read, and their types:**
  - `pid`: int, and it must equal the pid in the file name;
  - `sessionId`: non-empty str;
  - `cwd`: str;
  - `procStart`: a decimal str (a FILETIME in 100 ns units since 1601);
  - `kind`: str;
  - `status`: str;
  - `statusUpdatedAt`: int, epoch milliseconds.
- **An unusable file.** A missing field, a field of the wrong type, or a pid mismatch makes the file unusable. So does a file that cannot be read, bytes that are not UTF-8 (a write caught mid-character), text that is not one JSON object, or JSON nested deeper than the parser recurses: `json.loads` raises `RecursionError` there, not `ValueError`, and the reader catches it the same way. An unusable file is never a live session, and never absent either: the reader reports its name's pid in `unusable`, and R5 vetoes a pane tree holding that pid as `ambiguous-agent`. A name that is not an ASCII-digit pid names no process and is left out; a file that vanishes between the listing and the read is absent. An unusable file is never a raise that stops the sweep, and never an idle reading.
  - **Logged once per episode.** The latch is held per process and per file path. A file that parses again leaves it, so its next fault is logged afresh; a file that stays unusable is logged once, however many sweeps read it.
- **The stale-file rule.** A hard kill leaves `<pid>.json` behind with its last status. poc-reap2 saw `idle` after an idle kill and `busy` after a mid-turn kill (A4, C2).
  - The reader therefore returns a file only when a process with that `pid` is alive **and** its FILETIME creation time, read by **new** `procs.process_identity`, equals `int(procStart)` exactly, with no tolerance. poc-reap2 measured the two as equal (Setup).
  - A file whose pid is dead or reused is never trusted. It is dropped, with no log line, because a stale file is normal.
  - Claude Code later sweeps these files itself (poc-reap2 A5), but nothing here depends on that.
  - `read_session_files` is the **only** reader of this directory in magent. Any future reader (`status`, `watch`) goes through it.
  - magent never deletes, edits or cleans anything under `~/.claude`.
- **The quiet test.** `quiet` is true only for the `status` value `idle`. Every other value, `waiting`, `busy` and `shell` included, reads as not quiet.
- **Never read or logged.** The reader globs `*.json` only, so the `.key` files next to the session files are never opened. It never reads `name` into `LiveSession`, so the name cannot be logged.

**The pane state (R9).** R9 reads `fleet.classify_state` first.

- `busy` is the veto `pane-busy`, and `nopane` (an empty capture) is `pane-unreadable`.
- `dialog` is the veto `pane-dialog`. A dialog is a turn waiting on the user, and finished-only never parks one. It gets its own reason, not `pane-busy`, so `reap.log` shows how often it fires (see Risks, item 4).
- `idle` and `limit` go on to the draft check. `limit` is fine here because R6 and R7 have already required a turn that ended. A usage-limit notice on the screen of a finished session does not make it active.

**Draft detection (R9). New `fleet.input_draft(pane) -> str | None`:**

1. The input line is the last line that starts with the caret (`fleet.input_line`), with one exception. A caret line is a **menu option** when:
   - it matches `^\s*❯\s*\d+[.)]\s`, and
   - the nearest non-blank line either above or below it is a numbered option (`^\s*\d+[.)]\s`).

   A menu option is not the input line. So a lone numbered caret line, such as a draft that reads "1. fix the tests", counts as a draft.
2. If an input line is found, the input box runs from its top rule, the nearest rule line above the input line, to the next rule line below it, which must be the pane's last rule line. A rule line is a line made only of `─` that is exactly as wide, stripped, as the pane's LAST such line, the box's bottom edge. Claude Code draws the box the full width of the pane, so a narrower rule inside the box is draft text the user typed or pasted, not an edge. The box's width is attested by analogy, not captured: one read-only `capture-pane` of a live 50-column pane showed a dialog of numbered options, not the input box, and every rule in it was 50 wide; the input box is drawn by the same full-width layout. The claim that the box's bottom edge is the pane's last rule rests on the same analogy. If a future Claude Code draws a pane-wide rule under its footer, every idle pane's box closes at a rule that is not the last, so every idle pane reads `None` and the reaper never parks: fail-closed, never a false reap. The line right under the top rule is the prompt's caret line. Return the text after that caret together with every non-blank line below it in the box, each stripped. So a multi-line draft under an empty caret line is still a draft, and so is a draft whose last line is a lone caret: the last caret line need not be the box's first.
3. If there is no input line, the box has no top rule, the line under the top rule is not a caret line, or the box has no closing rule or closes at a rule line that is not the pane's last, return `None`, which is the veto `pane-unreadable`. That includes a numbered menu, since its caret lines are menu options, a capture cut off above or below the box, an empty capture (`""`), and a draft line made only of `─` and exactly as wide as the pane: it cannot be told from an edge, and closing the box there would leave the lines under it unread. Residual: such a line followed by a line that starts with the caret reads as the box's top, so the draft reads from there down, and as an empty box when nothing follows. It only bites on an unindented draft line, because a draft's later lines sit two columns in, under the text after the caret, so a rule typed there is two columns narrower than the box.

Under R9's order, the menu-option rule never decides a park: any caret line followed by a digit already makes `classify_state` read `dialog`, which vetoes first. The rule stays because `input_draft` is a public `fleet` function, and it must be right on its own: a numbered-option draft is a draft, and a numbered menu holds no input line.

**What counts as idle: finished only** (the user's decision, 2026-09-27). A session is idle only when its last turn **ended** and nothing is waiting on the user. The readings that count are: record states `done` and `idle` (R7), Claude Code status `idle` (R6), and pane states `idle` and `limit` (R9).

- `needs-input` is left out (R7 `record-state`), and so are Claude Code's `waiting` status (R6 `claude-busy`) and a `dialog` pane (R9 `pane-dialog`). A session in any of them is paused in the middle of a turn, on a permission prompt or a question. Killing it would abandon the pending tool call. poc-reap2 experiment C shows what an interrupted turn costs on resume: Claude Code adds synthetic "Continue from where you left off." / "No response requested." records and drops the turn, and it does not redo the work.
- `error` is left out. The Claude hook never writes it, so a record that says `error` came from a writer we do not know, and that is unknown.
- `working` is left out. The Stop hook writes `working` while the `background_tasks` ledger still lists running work. That is how background subagents and shells keep a finished-looking turn from being parked.

## The owner and the loop

**Owner: a new `serve` daemon thread, `upload_server._supervise_idle_reap(config_path, stop_event, interval=IDLE_REAP_INTERVAL_S)`.** It follows the precedent of `_supervise_psmux_priority`:

- **Serve is always up.** On a real machine `serve` is always running, while `attention -d` often is not (CLAUDE.md).
- **Serve runs on the desktop.** The Session-0 hand-off puts serve on the desktop, and a desktop process is what can open and terminate the fleet's processes.
- **Two owners would add nothing.** A second owner in `attention -d` would give nothing the lock does not already give.

**Thread behavior.**

- **Startup exits.** The thread returns at startup, after one log line, if any of these is false. The line is `idle reaper ` plus `reap.off_phrase(reason)`, the words doctor uses too, so "off" is said once: `idle reaper off (MAGENT_IDLE_REAP=0)`, `idle reaper off: non-interactive logon session`. An environment that does not validate adds its one WARNING first. The conditions:
  - `Platform.supports_psmux()`;
  - `Platform.logon_session_is_interactive()`;
  - the `MAGENT_IDLE_REAP` switch, which reads false on an environment that does not validate.

  These are `reap.process_off_reason(plat)`: `off_reason` without the setting, in the same order. None of them can change for a running process, so they are read once. This matches `_supervise_hotkey`. Nothing in the config ends the thread: `settings.idleReap` is read per sweep (step 2), so a config turned on later, or a broken one fixed later, needs no restart. A thread that stays logs `idle reaper on: sweeping every <interval>s` once.
- **The loop.** It waits `interval` first, then sweeps, and repeats until `stop_event` is set.
  - The first sweep comes one interval after serve starts. Every age comes from disk (the session file, the record, the transcripts), not from a timer in memory. A serve restart therefore neither delays nor speeds up a park.
- **One sweep.**
  1. It runs inside `with lockfile.exclusive_lock("idle-reaper")`, so two serve daemons on different ports never sweep at the same time. `LockHeld` skips the sweep with a debug line.
  2. It finds and re-loads the config (`config.load_config` on the path `paths.find_config` resolves for this sweep), so an edit to `settings.idleReap` takes effect without a restart. A config that fails to resolve or load (the working directory `find_config` reads deleted under serve, or a config missing, not JSON, invalid) skips the sweep with a warning that carries the error's text. The path is not resolved once at startup: there it would sit outside every guard, and a raise would end the thread before its first sweep while serve ran on.
  3. It calls **new** `reap.sweep_once(cfg, plat=plat)`, whose R1 gate applies the setting. When anything was parked, one INFO line gives the count and the total freed.
  4. Any exception is logged with `log.exception`, and the loop goes on. The reaper must never take down the server it rides on.
- **Wiring.** `run_server` starts the thread next to `_supervise_psmux_priority` and sets its stop event in the same `finally`.
- **Interval.** `IDLE_REAP_INTERVAL_S = 300.0`. X is at least 30 minutes, so a park lands between X and X + 5 minutes after the agent went quiet.
  - A sweep costs one `live_sessions` fan-out, one `pane_pids` fan-out, one Toolhelp snapshot and one read of the session directory, whose reader opens each file's process once to check its identity.
  - It also costs a few `stat` calls per session. `capture_pane` runs only for sessions that pass R1 to R8.

**`reap.sweep_once(cfg, *, tools=AGENT_TOOLS, config_dir=None, now=time.time, psmux_bin=None, plat=None) -> list[ParkResult]`** (**new**)

- **What the arguments are for.**
  - `tools` is the injectable agent registry. The image set and the idle probe come from it, and the e2e tier passes a stand-in here.
  - `config_dir` defaults to `sessions/claude.py::default_config_dir()`, which is resolved at call time, so the HOME redirect reaches it.
  - `now` is injectable, so no test sleeps for X.
  - `plat` defaults to `get_platform()`. R1's gate is `off_reason(cfg, plat)`, the same translation `doctor` reads. The thread's startup line reads its process half, `process_off_reason(plat)`.
- **Order and cap.**
  - Sessions that pass R1 to R9 are parked oldest-quiet first. "Quiet" here is the smallest of the R6, R7 and R8 ages.
  - At most `REAP_MAX_PER_SWEEP = 3` sessions are parked per sweep. The cap bounds the damage if an upstream change makes every session read idle at once. It counts the sessions tried, after the failed set is filtered out, so a session R10 spares still uses a slot.
- **Failed parks.** When a park aborts at a guard ("The stop", step 2) or cannot confirm the agent is gone (step 5), its agent identity (pid and create time) goes into an in-memory set, and that agent is never tried again by this serve process. A new agent in the same pane is a new identity, so it is eligible.
- **Logging.** Everything goes to **new** log name `reap` through `log.get_logger("reap")`, so it gets the shared multi-process handler for free.
  - Each park gets one INFO line. Its fields are listed in "The stop", step 8.
  - A session's veto reason is logged at INFO only when it differs from that session's reason at the previous sweep.
  - A session R10 spares is logged at INFO as `changed`, with what changed: `gone`, the new veto, or `another agent`.
  - Warnings and errors are covered in "Failure modes".

## The stop

**The stop is a hard kill, and only a hard kill.** Nothing is ever typed into the pane to make the agent stop. The decision and its evidence are in "Stopping the agent: hard kill vs clean exit" below.

**Record first.** The stop starts only after R10 passes. The reaper keeps the agent's `LiveSession` from that re-check: `session_id`, `pid`, `image` and `created`. Every later step uses these recorded values, because Claude Code can sweep the session file within a minute or two of the kill (poc-reap2 A5).

1. **Snapshot.**
   - Read the bound, **new** `procs.precise_filetime() -> int | None` (`GetSystemTimePreciseAsFileTime`; `None` off Windows), immediately before taking a new snapshot. Anything created at or after the bound is not provably the snapshot's and is dropped (below); a child the agent made between the read and the snapshot is picked up by the straggler pass. Measured in spec-reap-b2b: creation times are stamped from the precise clock, at least 0.6 ms after a read taken just before the spawn, and never before it.
   - Compute the listed tree: `process_tree(agent_pid, snapshot)`, root first. The root is the agent root (`claude.exe` here), found under `#{pane_pid}` by R4 and R5. **Nothing above the agent root is ever killed.** That includes the `cmd /c` wrapper and the pane shell.
   - Read `procs.process_identity(pid)` for every entry. **New**: this opens the process with `PROCESS_QUERY_LIMITED_INFORMATION` and reads two things:
     - the image base name, with `QueryFullProcessImageNameW`;
     - the creation FILETIME, with `GetProcessTimes`.

     It returns `None` off Windows, when access is denied, or when the process has exited.
   - **The kill list is what is proven, not what is listed.** Walk the listed tree root first. Keep an entry only if all three hold:
     - its identity reads;
     - it was created before the bound (`created < bound`). A later one is a newcomer that reused a listed pid after the snapshot, and `terminate_verified` would verify it as itself;
     - it was created after its kept parent (`created > parent.created`). Toolhelp never updates a parent pid: an orphan keeps its dead parent's pid, and whatever process reuses that pid "adopts" it. A process older than its listed parent was not made by it. spec-reap-b2b found one in 1 of 18 live `claude.exe` trees on the dev box.

     An entry that fails is dropped together with its whole subtree. Entries dropped because their identity is `None` (and their subtrees) are counted as survivors (step 2). Entries dropped by either time check are strangers: not ours, and not counted.
   - Residual: a system clock stepped backwards between the bound read and the snapshot defeats the bound.
2. **Guards.** Abort the park, log an ERROR, and add the agent to the failed set when any of these holds:
   - the bound is `None`;
   - the agent root's identity is `None`, or it differs from the recorded `image` and `created`;
   - the listed tree contains the pane pid;
   - the listed tree contains a `psmux.PSMUX_IMAGE_NAMES` image;
   - the listed tree is empty (the snapshot does not list the agent root).

   The pane-pid and psmux guards read the whole LISTED tree, strangers and unknowns included, not just the proven kill list: a stranger that matches refuses the stop. That fails closed, since it costs one park (the agent joins the failed set) and never a wrong kill.

   Children whose identity is `None` are left out of the kill and counted as survivors. **The pane shell is never killed.** It is an ancestor of the agent root, and the guard catches a pid-reuse cycle that would make it look like a descendant.
3. **Kill, bottom-up.**
   - Go through the kill list in reverse order. `process_tree` lists every process after its parent, so reversing it kills every child before its parent, and the agent root last.
   - Read the clock (`precise_filetime`) immediately before each kill and keep it with the pid. A kill that lands proves the pid was still that process after the read, so the read bounds its stragglers (step 4).
   - Call **new** `procs.terminate_verified(pid, expected) -> int | None` on each. It opens ONE handle with `PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION` and re-reads the image and creation time through that handle.
     - Only if both still match does it read the process's private commit bytes (`GetProcessMemoryInfo`, `PrivateUsage`) on the same handle and then call `TerminateProcess(handle, 1)`.
     - It returns those bytes (0 if only the memory read failed), or `None` when it killed nothing.
     - Holding the handle pins the process object, so the pid cannot be reused between the check and the kill. Reuse is real: poc-reap saw a pid reused within seconds, and poc-reap2 saw it again (pid 5392 went to an unrelated `sleep.exe`).
   - Measured in poc-reap2 A3: all 21 processes of a real subtree were terminated in 13 ms. The `cmd.exe` wrapper then exited by itself, with no "Terminate batch job (Y/N)?".
4. **Stragglers.**
   - Read a new bound, then take one new snapshot. With no bound there is no straggler pass: a straggler that cannot be bounded is never killed.
   - A straggler is any process whose parent pid is a pid we just killed, whose create time is:
     - later than that parent's, which means it is not an older process adopted through the parent's pid;
     - earlier than the clock read taken just before its parent's kill, which means the parent still held its pid. Once killed, the parent's pid is free, and a process that reuses it lists its own children under it. No read before that kill means no straggler under that parent;
     - and earlier than the new bound, which means it is not a newcomer that reused a straggler's pid.
   - Residual: a straggler the parent makes during its own terminate call, after the read, is neither killed nor counted as a survivor. That window is the terminate call itself. The tighter bound, a clock read inside `terminate_verified` after `TerminateProcess` while the handle still pins the pid, is known debt in DESIGN.md.
   - Stragglers are killed the same way, deepest first. This happens in one pass only. A straggler whose identity is `None` is not killed and is counted as a survivor.
5. **Confirm.** Poll every 0.25 s for up to `KILL_SETTLE_S = 5.0` until every recorded (pid, creation time) pair is dead.
   - If the agent root is still alive, the park failed. Log an ERROR, add the agent to the failed set, and write and type nothing. The pane is left as it is.
   - Any other survivors are counted and logged at WARNING. The park still counts.
6. **Re-walk, then reset.** Re-walk the pane with `psmux.idle_sessions([sid], images=agent_image_names(tools))`, so the image set is the one the sweep was given. It reads `#{pane_pid}` again, walks the pane's subtree from a new snapshot, and ends with the console-membership check (next subsection). Only when it reports the pane idle is the reset typed. That call is both gates at once: nothing agent-like is under the pane shell, and nothing outside the pane shell's subtree shares its console.
   - The re-walk is polled every `_RESET_POLL_S = 0.25` s for up to `RESET_SETTLE_S = 5.0`, because the `cmd` wrapper and the venv launcher exit a moment after the agent dies. Every poll is a full re-walk with its own console check, so no poll leans on an earlier one's answer.
   - The typed text is the line built by **new** `Platform.pane_reset_command(shell_image, notice) -> str | None`. The ABC default is `None`.
   - `WindowsPlatform` returns this line for the `pwsh` and `powershell` stems. It is the exact extended reset from poc-reap2 A4. The only change is that the notice is quoted with the existing `platform/windows.py::_ps_quote`:

     ```
     $e=[char]27; [Console]::Write("$e[?1000l$e[?1002l$e[?1003l$e[?1006l$e[?1004l$e[?2004l$e[<u$e[>4;0m$e[?1049l$e[?25h"); Clear-Host; Write-Host <_ps_quote(notice)>
     ```

   - The line is sent with `fleet.paste_and_enter`.
   - poc-reap2 measured that this line sets `#{alternate_on}` to 0. psmux gives no usable answer for `#{cursor_flag}`, `#{mouse_any_flag}` or `#{bracket_paste_flag}`, so none of them is read to confirm the reset.
   - If the re-walk is unknown (one that raises included), finds an agent, or fails the console check, or if the shell has no reset line, then nothing is typed and a WARNING is logged. The pane keeps the dead frame until someone clears it or resumes. This costs looks only: resume repaints the whole frame without the reset (poc-reap2 C1). The park still counts.
7. **Record.**
   - Write `agent_state.write_state(session.cwd, PARKED, session_id)`, using the recorded `session_id`.
   - No SessionEnd hook runs on a hard kill (poc-reap2, "Other leftovers"), so this write is the only record of the outcome.
   - If the write fails, log a WARNING. The notice still names the id, unless the resume command holds a control character: then the notice carries our words only (see "The notice"), and `reap.log` is the only place the id survives. Its WARNING holds the escaped resume command, `--resume <id>` included.
8. **Log.** One INFO line, with these fields:
   - the psmux session;
   - `sessionId`;
   - the agent's pid, image and creation time (FILETIME);
   - the idle duration (the smallest of the R6, R7 and R8 ages);
   - the processes killed and the survivors;
   - `freed≈<MB>`. This is the sum of the private commit bytes `terminate_verified` returned. It is an estimate: it leaves out shared pages, survivors and orphans.

**The notice** is one line: `magent: parked after <N> min idle to free memory. Resume: <resume command>  (or magent status, r<n>)`. The resume command is `sessions.build_resume_command(tool, p["cmd"], session_id)`, the same string the resume path types. For Claude, this keeps the configured flags, removes `--continue`, and appends `--resume <sessionId>`. Every other character of the configured `cmd` is carried verbatim, so the line is not ASCII in general. It is still ONE single-quoted PowerShell literal: `_ps_quote` doubles all five code points PowerShell ends such a literal on (U+0027, U+2018, U+2019, U+201A and U+201B), and a test pins that with PowerShell's own parser. The line is typed as keystrokes, so a control character in it would act as one (a CR submits, a tab completes, an ESC starts a sequence). A resume command holding any Cc code point (C0, DEL or C1) is therefore left off the line: the pane gets our words only, `Resume: magent status, r<n>  (the resume command holds a control character, see reap.log)`, and the command goes to `reap.log` escaped (`%r`), at WARNING. Every other character stays verbatim. The configured `cmd` is where such a character can come from: the session id is already bounded by `SESSION_ID_RE` (`sessions/live.py`, applied by Claude's session reader), so checking it here, and logging it with `%r` on the park's INFO line, is defence in depth.

**Leftovers magent leaves alone.** A hard kill leaves these behind:
- the stale `sessions/<pid>.json` and its `.key` (the stale-file rule makes the stale file harmless);
- 7 plugin `.in_use/<pid>` markers per reap;
- `session-env/<sid>/`;
- `security_warnings_state_<sid>.json`.

The last two also stay after a clean exit. Claude Code already leaves `.in_use` markers behind fleet-wide: 84 of the 112 in one plugin were stale (poc-reap2). **magent deletes none of these.** They are Claude Code's own undocumented files, and a wrong delete could hide a live session.

**Orphans.**
- **An orphaned child of the agent** is not chased in v1. That is a process whose parent had already exited before the snapshot. The kill cannot reach it, because both the parent-pid walk and `taskkill /T` miss it (poc-reap). Survivors are counted in the log line, so the memory left behind is measured, not assumed.
- **An orphaned agent** is a different case: a `claude.exe` whose `cmd` wrapper was killed out of band. R5 finds no agent in the pane's tree, so the reaper kills nothing (`no-agent`). The console-membership check below keeps every keystroke out of that pane.

### Before any keystroke: the console-membership check

**The hole.** `idle_sessions` decides "no agent in this pane" by walking `#{pane_pid}`'s parent-pid subtree. An orphaned agent is outside that subtree but still attached to the pane's console, so it shares the pane's input (`sp-revive.md` item 1). Anything typed into the pane can land in it, and that covers the reaper's reset, `r<n>`, and every bulk revive.

**The closure.** This is a prerequisite that the reaper plan builds before the reset exists.

- **New `procs.console_clients(pids, *, timeout=CONSOLE_PROBE_TIMEOUT_S) -> dict[int, frozenset[int] | None]`**:
  - **One helper.** It spawns ONE helper, `[sys.executable, "-c", <stdlib ctypes source>, <out file>, *pids]`, with `DETACHED_PROCESS`, so the helper has no console of its own.
  - **Per pane.** For each pid, the helper runs `AttachConsole(pid)`, then `GetConsoleProcessList`, then `FreeConsole`. Its own pid is left out of each list.
  - **Output.** It writes one JSON object to `<out file>`, a path in a fresh `tempfile.mkdtemp` directory. It uses a file because the helper's standard handles are not reliable once it has swapped consoles; that is the reason the prototype `poc-reap/conlist.py` wrote to a file. The caller reads the file after the helper exits and removes the directory.
  - **Failures.** A pid whose attach fails gets `None`. So does every pid when the spawn fails, the helper runs past `CONSOLE_PROBE_TIMEOUT_S = 5.0` (it is then killed), or the output is missing or unparsable. Off Windows every pid gets `None`.
  - **Own console untouched.** magent never calls `AttachConsole` or `FreeConsole` in its own process. Doing so would swap the console of `serve` or of the CLI.
- **Both gates, one call.** Nothing is typed into a pane (the reset with its notice, `r<n>`, a bulk revive) unless BOTH gates pass:
  - revive's tree rule: the pane's own process is a shell, and nothing under it is an agent image or a live launcher;
  - the console-membership check.

  Any failure in either one means no keystrokes, and it is logged. The check is the last stage of `idle_sessions`, so one call asks both, and no caller can ask one without the other.
- **It is the last stage of `psmux.idle_sessions`.**
  - For the panes that passed every tree stage, it makes one `console_clients` call.
  - A pane stays idle only if all of these hold: its client set is not `None`; every client pid is in the pane's own subtree (the pane shell included); and no client's image is an agent image or a launcher (the `running` set).
  - Anything else leaves the pane not proven idle and logs one WARNING (psmux's `launch` log) naming the pane and the reason.
- **Why inside `idle_sessions` and not next to it.** Every path that types into a pane already asks `idle_sessions` first. That covers `revive_sessions` (bulk and `r<n>`), the bring-up send-verify re-send in `platform/windows.py`, and the reaper's reset. Putting the check inside makes it impossible to type while skipping it. It also means the reaper reuses the seam instead of building its own.
- **Cost.** At most one helper spawn per `idle_sessions` call, and only when at least one pane survived the tree stages. On a busy fleet that is nothing. `status` pays it when a dead or parked pane is listed.
- **What it does not cover.** An orphaned agent on a different console cannot receive the keystrokes. A revive there would still start a second agent on that directory. This lesser hazard is documented; it is not closed.

### Stopping the agent: hard kill vs clean exit

**Decision: a hard kill only, gated on real idleness (every row of the decision table). A clean exit by typing is rejected.** The evidence is `poc-reap2.md`, a real Claude Code 2.1.280 in a scratch psmux server and cwd:

- **A. Hard kill while idle (the reaper's case).**
  - A bottom-up `TerminateProcess` of all 21 subtree processes took 13 ms.
  - The transcript was byte-identical afterwards, and every line was valid JSON.
  - `claude --resume <sessionId>` in the same pane appended to the same file. Asked what it replied last time, it answered correctly (`OK-1`).
- **B. Clean `/exit` while idle.** The gains are housekeeping only:
  - it removes `sessions/<pid>.json`, its `.key` and the `.in_use` markers;
  - it appends 4 bookkeeping records;
  - it runs SessionEnd;
  - it leaves the alternate screen;
  - it prints a resume hint.

  Nothing about the conversation is better. And it requires **typing into the pane**: an Enter can answer an open dialog, text lands in a draft, and a mangled `/exit` once went to the model as a prompt (B1). That is the dangerous verb this design keeps to one guarded place, and the stop does not need it.
- **C. Kill in the middle of a reply (not the reaper's case).**
  - The transcript was not torn: Claude Code writes whole records.
  - On resume, Claude Code added synthetic "Continue from where you left off." / "No response requested." records and dropped the interrupted turn. It did not redo the work.

  **The idle gating makes this case unreachable.** While a reply streams, Claude Code's status reads `busy` (R6 `claude-busy`), magent's record reads `working` (R7 `record-state`), the transcript is being written (R8 `transcript-recent`), and the pane reads busy (R9 `pane-busy`). Any one of them vetoes, and all must have read quiet for longer than X (at least 30 minutes). R10 re-reads every row for the same pid and creation time just before the kill, which closes the window between the sweep's first read and the stop. A turn paused on the user is not reapable either (finished only): it reads `waiting` (R6), `needs-input` (R7) or a dialog pane (R9), and each one vetoes on its own.

## Parked state and resume

**The state.**

- `agent_state.PARKED = "parked"` (**new**) joins `_VALID`.
- `RECORD_VERSION` goes from 1 to 2. Adding a state fails `tests/unit/test_agent_state.py::TestSchemaContract::test_valid_states_match_module_constants`, and that file's rule for a failing contract is to bump the version in the same commit.
- The record's key set (`state, ts, cwd, session_id`) and its value types do not change.
- `parked` has exactly one writer, the reaper. A new contract test asserts that `state_hook`'s event map (`_CLAUDE_EVENT_STATES`) and its Codex handler never produce it.

**Compatibility with v1 writers and readers.**

- **Nothing on disk carries a version.** A record has no version field, and no reader checks `RECORD_VERSION`. So a v1 record is also a valid v2 record. v2 adds one value, `parked`, to `state`, and nothing else.
- **v1 writers keep writing, and readers must keep accepting them.** Two writers stay on v1 until they are upgraded:
  - this machine's installed hook writer, the `state_hook` of the `py -3.14` magent;
  - the Codex `notify` recipe.

  Every v2 reader must accept any record in the v1 vocabulary exactly as it does today. No reader may skip, reject or re-rank a record for lacking something only a v2 writer produces. The reaper's own reader, R7, is one of them: it reads the state value and the key set, both of which v1 already has.
- **A v1 writer can overwrite a `parked` record.** The store keeps one record per cwd, and every write replaces it.
  - The expected case is a resume. The resumed agent's SessionStart hook, v1 or v2, writes `idle` over `parked`. That is correct, because the session is no longer parked.
  - The same overwrite is what ends the parked record after a resume typed by hand, since nothing else clears it then.
  - Any other writer for the same directory ends it too, for example a Claude Code the user starts by hand in that directory, outside magent. The pane then reads as an ordinary dead pane (see Risks, item 9).
- **An older magent reading a `parked` record** (for example a second install sharing `~/.magent`) sees an unknown state:
  - `attention` sorts it last (`_URGENCY.get(state, 99)`) and raises no push;
  - it gets no title badge;
  - `watch` prints it unstyled, and the session picker shows no label.

  That older magent's revive has no `resume_parked`. It treats the pane as an ordinary dead one and types the configured command. Only an upgraded magent resumes by id.
- **The contract-test change** (`tests/unit/test_agent_state.py`):
  - `VALID_STATES` gains `"parked"`. `EXPECTED_KEYS` is unchanged.
  - **New** `test_record_version_is_2`.
  - **New** `test_a_parked_record_has_the_v1_keys_and_types`: `write_state(cwd, "parked", "sid")` produces exactly `EXPECTED_KEYS`, with the same value types.
  - **New** `test_a_v1_record_still_reads`: a committed record in the v1 writer's exact bytes reads back unchanged through `state_for` and `all_states`.
  - **New** `test_a_v1_write_replaces_parked`: after `parked`, `write_state(cwd, "idle", "new-sid")` leaves `idle` with `new-sid`.
  - The module docstring's rule 3 ("update any external writer in lockstep") gains one sentence: a state that only magent writes is additive, so v1 writers stay valid and need no change.
  - In `tests/unit/test_state_hook.py`: no Claude event and no Codex notify payload produces `parked`.

**Readers.**

- `attention._URGENCY["parked"] = 5`. It sorts after `idle`.
- It gets no staleness entry, so it never ages.
- It is not in `PUSH_STATES`, so it never raises a toast, an ntfy push or a taskbar flash.
- `titles.STATE_BADGES` is unchanged. A parked window's title carries no badge. Parked is quiet, like `idle`.
- `session_picker._status_label` and `watch._state_label` show `parked` dimmed.

**Resume. It is always `--resume <sessionId>` and never `--continue`.**

- **The one entry point.** Every resume goes through `psmux.revive_sessions(config, only=None, group=None, *, resume_parked=False)`. The new keyword argument (**new**) sits on the function the revive branch already fixed.
- **Checking for a parked record.** It considers only live candidates that `idle_sessions` calls idle, which now includes the console-membership check, so nothing is typed into a pane an orphaned agent shares. For each one it reads `agent_state.state_for(p["resolved"])`.
- **When the record is `parked` and its `session_id` has the shape a session file may hold** (`sessions.live.SESSION_ID_RE`, full match; the id is typed into a shell, so a non-empty test is not enough):
  - with `resume_parked=True`, it types `cmd /c <build_resume_command(tool, p["cmd"], session_id)>` + Enter (the injection shape `revive_sessions` already uses), and on a successful send it calls `agent_state.clear_state(p["resolved"])`. The agent's own SessionStart hook then writes the live state.
  - with `resume_parked=False`, the pane is left alone. Nothing is typed.
- **A `parked` record with no usable `session_id`** (missing, empty, or any other shape). The pane is left alone and a warning names it. It never falls through to `--continue`.
- **Any other record.** Today's behavior is unchanged: the configured command is typed.
- **Who passes what.**
  - `cli/status.py::_revive_session`, the `r<n>` action, passes `resume_parked=True`. When nothing is revived it prints the reason `revive_sessions` reports through its `vetoed` mapping, in the words of the check that stopped it (a parked pane with no usable id says so, and a pane that is not proven idle is not claimed to hold a running agent).
  - `launch.revive_psmux` gains the pass-through keyword with default `False`. Both bulk paths use that default: interactive `magent up`, and `magent up --json --revive`, which `magent attach` runs on the host. **A bulk revive therefore never resumes a parked session.** The team lead confirmed this ruling. Automatic resume is out of scope, and resuming on every attach would undo the saving.
  - Origin/main has no `magent revive` command. `revive_sessions` is the one function every revive path reaches, so changing it covers them all.
- **Resuming by hand.** The user can type the command from the notice directly at the pane's prompt. Nothing clears the parked record in that case. The agent's SessionStart hook overwrites it with `idle` as soon as the agent starts.

**How the user learns how to resume** (the least intrusive way).

- The notice line in the pane itself, shown exactly where they look when they come back.
- The `parked` label in `status` and `watch`.
- No title badge (the titles grammar has none for quiet states).
- No status-bar change or flash (status bars are ASCII-only and shared by every attached client; a flash is gone before anyone reads it).

## Config and kill switch

**Config: `settings.idleReap`** (**new**)

- In JSON it is `{"enabled": true, "afterMinutes": 120}`.
- It parses to **new** `config.IdleReapSettings(enabled: bool = True, after_minutes: int = 120)` on `Settings.idle_reap`.
- It is wired into `_parse_settings`, `settings_to_dict`, `_ALLOWED_SETTINGS_KEYS`, and a new `_ALLOWED_IDLE_REAP_KEYS = {"enabled", "afterMinutes"}` (unknown keys warn, as they do today).
- **Floor.** `reap.threshold_s(cfg)` applies it. It returns `max(after_minutes, 30) * 60` and logs once per process when it raises the value. `load_config` stays pure and quiet.
- **No `SCHEMA_VERSION` bump.** Absent keys parse to their defaults, as `attention` did. `migrate_config_file` does not change.
- **Example config.** `magent.config.example.json` gains the block in the same commit, or the drift pin (`test_config_factory.py::TestExampleConfigMatchesFactory`) fails.
- **On by default,** as the brief decided.

**Kill switch: `MAGENT_IDLE_REAP`** (**new** `MagentEnv.idle_reap: bool = True`)

- **`reap.off_reason(cfg, plat)` is the one gate** that the sweep and `doctor` read; the thread's startup reads its process half, `process_off_reason(plat)`. The setting and a switch of 0 each turn reaping off, so a value of 0 overrides the config.
- **An env that fails validation turns reaping off,** with its one WARNING and then the one startup line. This deliberately departs from `psmux.boost_enabled`'s "degrade to the default" rule. The doctrine's point is to keep the owning process alive, and returning False keeps it alive too. The reaper is the one supervisor whose verb is destructive, so here the reaper fails closed. Its off reason says so: "off (the MAGENT_* environment did not validate)", never the "off (MAGENT_IDLE_REAP=0)" of a deliberate 0.
- `.env.example` gains a commented `# MAGENT_IDLE_REAP=1` block. Its drift pin is `test_env_schema.py`.

**Test-isolation law: `MAGENT_IDLE_REAP=0`.**

- **Every tier.** `tests/conftest.py` pins `MAGENT_IDLE_REAP=0` with `monkeypatch.setenv`, next to `MAGENT_PSMUX_BOOST` and after the `MAGENT_*` sweep, with a comment that gives the reason below. It covers `tests/platform/` too, as `MAGENT_PSMUX_BOOST` does.
- **Every child `env=`.** Every fixture that builds an explicit child `env=` carries `MAGENT_IDLE_REAP=0` beside `MAGENT_HOTKEY_SUPERVISOR`, `MAGENT_UPLOAD_SUPERVISOR`, `MAGENT_PSMUX_BOOST` and `MAGENT_SESSION0_POLICY`. On `bf67c16` that means all 27 test modules that set `MAGENT_PSMUX_BOOST`: 3 in `tests/dist/`, 17 in `tests/e2e/` and 7 in `tests/platform/`. Among them is every fixture that starts a real `serve`, and `serve` is where the reaper thread lives.
- **Turning it back on.** Only tests that are about the reaper do this, and only in process: `monkeypatch.setenv("MAGENT_IDLE_REAP", "1")` plus `monkeypatch.setattr("magent.env._cached_env", None)`, the pattern `test_psmux_boost.py` uses. The real-multiplexer tier (`tests/e2e/test_reap_real.py`) does this only after its `bring_up` returns and only for its in-process `sweep_once` and `revive_sessions` calls. It starts no `serve` and no `attention -d`, and any child `env=` it builds carries 0.

This is the sharpest law of the set. The reaper is the only code in the product that **terminates** processes it did not spawn. It reaches them through psmux session names and `~/.claude` session files, and no HOME redirect contains either the psmux registry or a live fleet.

## Visibility

| Surface | Change | Why |
|---|---|---|
| The pane | The notice line (see "The stop") | It is the one place the user is guaranteed to look, and it carries the exact resume command. A resume command holding a control character is the exception: the notice carries our words only, and `reap.log`'s WARNING holds the command escaped. |
| `status` (human) | The existing state column shows `parked`. The `r<n>` action resumes a parked session by its id. No new daemon line. | The session table already shows each session's state, so parked needs only a new label. The reaper lives inside `serve`, and serve's health is already reported. |
| `status --json` | No new key. `psmux_sessions[].state` and `agents[].state` can now read `"parked"`. That value is additive and documented. Exit codes are unchanged, because parked is healthy. | No consumer needs a reaper-level block today (YAGNI). |
| `watch` | A `parked` label (dimmed), sorted after `idle`. | Without the label it would render as unstyled raw text. |
| `doctor` | A new `idle-reap` check. OK: "on, parks after N min idle", or the `off_reason` phrase that names the gate that is off, through `reap.off_phrase` like serve's startup line ("off in settings.idleReap", "off (MAGENT_IDLE_REAP=0)", "off (the MAGENT_* environment did not validate)", "off: unsupported platform (no psmux)", "off: non-interactive logon session"). A configured `afterMinutes` under the floor adds "(afterMinutes=N raised to the 30-min floor)"; `load_config` stays quiet, so doctor is where that shows. **WARN** when reaping is on but the state hook is not wired for Stop, Notification, UserPromptSubmit and SessionStart. It uses `cli/hooks_cmd.py`'s `_load_settings` and `_event_wired`. The WARN reads: "nothing will ever be parked; run magent hooks install". An unreadable `settings.json` is a WARN that names the file. With no loadable config the check is a WARN: "config invalid or missing; the reaper stays off until it loads" (the config check above it already fails). | This check earns its place. Reaping is on by default while the hooks are opt-in (`magent hooks install`). Without the check, an unwired machine has a reaper that is on and silently does nothing, and R7 vetoes every session. That is the same silent failure that left the hook dead for two months. WARN is the worst it ever reports. |
| `reap.log` | Parks, reasons (logged when they change), warnings and errors | This is the audit trail for a destructive feature, and it is how a veto that always fires, such as a fleet-wide transcript touch, becomes visible. |

## Failure modes and the inv-unknown posture

The posture is the one from inv-unknown ("unknown never reads as absent or dead"), turned around for this feature. **Unknown never reads as idle.** Every row below leads to no park, except where the table says otherwise.

| Failure | Result |
|---|---|
| Not Windows (`snapshot_processes` returns None), or the snapshot fails | R4 `tree-unknown` |
| The pane pid cannot be read, or the pane process is gone | R4 `tree-unknown` |
| The pane is in another logon session (Session 0, or created over ssh), or its processes cannot be opened | R4 `other-logon-session`, or R5 `no-agent` (the reader cannot verify the file's process) |
| The session directory cannot be read; `kind` is not interactive | R5 `no-agent` |
| A session file for a pid in the pane's tree is there but unusable: it cannot be read, is not UTF-8, is not one JSON object, is nested past the parser's depth, has a field missing or of the wrong type, or names another pid | R5 `ambiguous-agent`, never `no-agent`, whether or not another agent in the tree reads cleanly. Logged once per file per episode. An unusable file whose pid is not in this tree says nothing about this pane |
| A stale session file: its pid is dead, or reused (`procStart` is not the creation time) | The reader drops it (the stale-file rule), so R5 `no-agent` |
| No record, a record for another session, or a record older than the process (for example a dead hook) | R7 |
| The record's `ts` is missing, not a number, non-finite, or an int past any float | R7 `record-unreadable`, even when the agent's start time reads as 0 |
| The record file is there but unusable: it cannot be read, is not UTF-8 JSON, is not an object, or is nested past the parser's depth (`json.loads` raises `RecursionError` there, not `ValueError`) | R7 `record-unreadable`, never `no-record`. The reader never raises, so the other sessions in the sweep are still judged |
| The main transcript is missing (for example, the path encoding changed upstream) | R8 `no-transcript` |
| `capture-pane` fails or times out | R9 `pane-unreadable` |
| Another serve holds the lock | The sweep is skipped |
| The config fails to load | The sweep is skipped (WARNING) |
| An exception inside a sweep | `log.exception`, then the next interval |
| The agent survives `TerminateProcess` | No park: ERROR, the agent goes into the failed set, and nothing is written or typed |
| The agent is gone but some descendants survived (access denied) | Parked. A WARNING gives the survivor count |
| After the kill, the re-walk is unknown, finds an agent, or fails the console check | Parked. Nothing is typed (WARNING) |
| The console helper fails, times out, or cannot attach, on any typing path | That pane is not proven idle, so nothing is typed into it (WARNING in `launch.log`) |
| The reset `send_keys` fails | Parked. WARNING |
| The parked write fails | Parked in fact; the record is missing. WARNING. The notice still names the id, unless the resume command also holds a control character: then `reap.log` is the only place the id survives, in the WARNING's escaped `--resume <id>` |

ERROR lines reach Sentry through the existing logging integration when a DSN is set.

## Dependencies and stacking

1. **`fix/state-hook-module-entry` lands on main first.** On this machine the hooks were rewired to the module form on 2026-07-28, and that form has no `__main__`, so no record has been written since then (`impl-hookmain.md`).
   - The fix also has to reach the installed `py -3.14` magent before any record appears. The user decides whether that comes from a release or a local install.
   - Without it, R7 vetoes every session, so the reaper fails safe. It still must not ship first, because a reaper that can never act is a broken feature.
2. **`fix/revive-live-agent` lands on main.** At `bf67c16` it provides:
   - the one "is an agent alive in this pane" seam, `psmux.idle_sessions`, which reports "unknown" as not idle;
   - `psmux._LAUNCHER_IMAGES`: a live `cmd` under the pane shell means the launched command is still running, so tools outside the registry (agy, cursor-agent) read not idle;
   - the three-field `procs.snapshot_processes`;
   - `procs.process_tree`;
   - `psmux.pane_pids`;
   - `AgentTool.images`, `AGENT_RUNTIME_IMAGES` and `agent_image_names`.
3. **`feat/idle-reap` branches from main after both.** Its first two commits change the shared seam before any reaper code exists:
   1. **A behavior-preserving refactor.**
      - It extracts `psmux.pane_trees(names, psmux=None) -> dict[str, list[tuple[str, int, int]] | None]`, which makes one `pane_pids` fan-out and one snapshot.
      - It adds `images: frozenset[str] | None = None` to `idle_sessions`. The default is `agent_image_names()`, and `_LAUNCHER_IMAGES` is always added.
      - It adds `tools: Mapping[str, AgentTool] = AGENT_TOOLS` to `agent_image_names`, so the reaper builds the image set from the registry it was handed.
      - `idle_sessions` becomes: the foreground filter, then `pane_trees`, then a check that the root is a shell, then no running image in the tree.
      - The revive branch's tests must pass **unchanged** after this commit. That is the proof that the refactor preserves behavior (18 of 18 mutants were killed there).
   2. **The console-membership stage.** It adds `procs.console_clients` and the last stage of `idle_sessions` ("Before any keystroke" in "The stop"). This is the prerequisite from `sp-revive.md` item 1. It lands before the reset exists, and it hardens bulk revive and the send-verify at the same time.

   After these two commits, the reaper reads `pane_trees` for R4 and `idle_sessions` before it types anything. It never walks a pane a second way.

## Testing

No test may reach the live fleet. Every psmux session a test drives has a unique per-test name stem, and the config each sweep receives lists only those sessions. Every process a kill test terminates is one the test spawned itself. The real-process stop tests hand `_stop` a table cut down to their own pids, and their kills go through a guard that fails the test on any process it did not register, before the OS sees it. The guards in `tests/conftest.py` sit under every test in every tier, so no module is outside them:

- `_stop`'s default snapshot fails the test instead of walking the live table (`@pytest.mark.live_process_table` opts out, for a tier that must walk it).
- Every in-process `procs.terminate_verified` refuses a process the test did not register in `own_pids` (no opt-out; a test that kills registers what it spawned). The key is the identity (pid, image and creation time), not the pid: a registered process that died leaves its pid free, and the real `terminate_verified` would verify a stranger reusing it as itself.
- Under it, the kernel32 that `procs` hands out lets `TerminateProcess` land only inside that guarded call, on a handle opened for the pid it approved, and never lets `TerminateJobObject` land. `procs` builds other kernel32 handles of its own, so its source is pinned too: `TerminateProcess` is declared in `_kernel32` and called only in `terminate_verified`, `TerminateJobObject` is named nowhere, and only `terminate_verified` opens a handle with `PROCESS_TERMINATE`.
- `os.kill`, `taskkill` and psmux `kill-server` cannot be refused under every tier, because e2e teardown uses them on its own daemons. Reap's reach is pinned from its source instead, as closed lists that go red on anything new:
  - What it takes from the stdlib is a closed list of (module, name) pairs: the names a `from` import binds, and the members it reads off a module imported whole, which it loads only as `module.name`, keyed by the name the import binds (`import http.server` binds `http`, so `http.server.os` is a read of `http`). The pairs are `__future__.annotations`, `collections.abc` `Callable`, `Mapping` and `Sequence`, `logging.Logger`, `math.isfinite`, `pathlib.Path`, `time` `monotonic`, `sleep` and `time`, `typing` `NamedTuple` and `TYPE_CHECKING`, and `unicodedata.category`. None of them ends a process, and none reaches a member by a string (`operator.methodcaller`, `inspect.getmembers`, `pkgutil.resolve_name` and `typing.get_type_hints`, which evaluates a string annotation, each can).
  - What it imports from magent is a closed list of names, because another module can end a process (`launch.stop_psmux`, `upload_server.stop_server`). Every one is a `from magent... import`, never an `import magent...` (plain or aliased, which hides the module from the member scan), a relative import, an `importlib` import or a `__import__`.
  - Each of the seven modules among them (`agent_state`, `config`, `env`, `fleet`, `log`, `procs`, `psmux`) is loaded only to read one member: `psmux.x`, never `m = psmux`, `(psmux,)`, `f(m=psmux)` or `f(psmux)`. The members it reads are a closed list per module, and none of them is a name that module imported. Of those members, `terminate_verified` is the only one that ends a process, and no `procs` name is bound past the guard's patch.
  - A Platform is reached through a value, so what reap asks of one is pinned by attribute name, whatever holds it. Of every attribute of `Platform` and of its subclasses in the platform package (methods, class attributes and `self.` attributes, private ones included), reap spells exactly `supports_psmux`, `logon_session_is_interactive` and `pane_reset_command`.
  - Reap names none of the builtins that reach a member by a computed name (`getattr`, `setattr`, `delattr`, `vars`, `globals`, `locals`, `eval`, `exec`, `compile`), and spells no dunder, as an attribute or as a bare name (`__dict__` and its kind hand out a module's namespace, and `__builtins__["__import__"]` is `__import__`). Every attribute chain rooted at a name it bound from magent is one link deep, so `agent_state.os.system` cannot pass, and `agent_state.os` alone fails the member list. It imports none of `sys`, `builtins` or `gc`, each a door to every loaded module.
  - `procs` is pinned the same way at its source. Its imports are exactly `__future__`, `collections.abc`, `ctypes`, `json`, `os`, `shutil`, `subprocess`, `sys`, `tempfile` and `typing`. That list goes red when any change adds an import to procs, which is intended: whoever merges it extends the list, in review. Five process enders of kernel32, ntdll and user32 (`TerminateProcess`, `TerminateJobObject`, `NtTerminateProcess`, `ZwTerminateProcess`, `EndTask`) are named only where declared and called, whether by attribute, in a string or bytes constant, or in what the scans fold: a `+` chain of constants, `sep.join` over a literal tuple or list of them with a constant `sep`, and an f-string of them with no format spec and no conversion but `!s`. Of the computed-name builtins above it uses one, `getattr` on `sys` in `_helper_python`, never binds one under another name, and spells no dunder. Its other kill names (`kill`, `killpg`, `terminate`, `send_signal`, `pidfd_send_signal`) appear at a closed list of sites, each a whole call, a bare load or an import: `pid_alive`'s `os.kill(pid, 0)`, which only probes, and `console_clients`' `proc.kill()` of the helper it spawned. No string or `+` chain of it spells `taskkill`, one of those kill calls or `os.kill`.
  - The console helper is Python that procs runs as a child, so it is parsed too (a helper that stops being Python fails) and gets the same scans with its own lists: no kill site, no ender, no `taskkill`, none of the computed-name builtins, no dunder, and imports of `ctypes`, `json` and `sys` only, with one `from` import (`from ctypes import wintypes`). Each module it binds is read only as `module.name`, from a closed list: `ctypes` `POINTER` and `WinDLL`, `ctypes.wintypes` `BOOL` and `DWORD`, `json.dump` and `sys.argv` (`sys.modules` hands out any module by name, and `ctypes._os` is `os`). That list goes red when any change reads a new member (`wintypes.HANDLE` or `ctypes.byref` for a new console call), which is intended: whoever merges it extends the list and this spec, in review. Its kernel32 is closed the same way: `WinDLL` is read exactly once off ctypes, found by the name ctypes is bound to rather than by its spelling (so `import ctypes as c` or `W = ctypes.WinDLL` is a second read), and that read is called at once and bound once to one name, which is read only as `k.member` (never `k["name"]` or `k2 = k`), and the members read along those chains are exactly `AttachConsole`, `FreeConsole`, `GetConsoleProcessList`, `GetCurrentProcessId`, `argtypes` and `restype`. The enders are a deny list, and the helper attaches to an agent's console, where `GenerateConsoleCtrlEvent` would end the agent without `TerminateProcess`.
- **What the pins do not close.** The pins close the routes they name, and nothing more. What a named member does is reviewed where that member changes (a kill added inside `agent_state.write_state` passes every pin here), and procs' kernel32 is pinned at its source, against the five enders named above only: a kernel32 call that ends a process by another name (`_kernel32().GenerateConsoleCtrlEvent`, `DebugActiveProcess`) passes the procs scans. Only the console helper's kernel32 is a closed list. An evasion through `eval` or `exec` of text built at run time, a name built from a run-time value (an f-string or a `join` over one), a name built from constants in a way the scans do not fold (a `join` over a generator, `str.join("", ...)`, `%` formatting, or an f-string with a format spec such as `f"{'task':s}kill"`), a ctypes function pointer made from an address, or any other route the pins do not name is a review matter. The runtime guards exist only once `tests/conftest.py` has run: a kill at module level in any magent module would run at import, during collection, before any guard exists.

The guards are pinned by what they do, from a module that is not reap's: a foreign pid in the injected table is never killed, a reused pid of the test's own is refused, the default snapshot fails, and a renamed `snapshot` keyword fails the guard instead of passing it.

- **Decision table** (`tests/unit/test_reap_decide.py`, **new**).
  - Start from one passing `Signals` value, then flip one field per case: one case per veto reason, plus "all pass → reap".
  - The finished-only cases, one each: Claude Code status `waiting` gives `claude-busy`; record `needs-input` gives `record-state`; pane `dialog` gives `pane-dialog`; pane `limit` with every other row passing reaps.
  - Boundaries: an age equal to X does not reap; an age strictly older than X is required. A NaN in any time field vetoes, and so does a NaN a row makes itself: `now` and `record_ts` both infinite make the record-recent row's age `inf - inf`.
  - An unknown record time (`None`) vetoes as `record-unreadable` with an agent start time of 0 and of a real epoch, in `decide` and through gather's real record read. In `quiet_s` it is no age (0 seconds), never an old one.
  - An unusable record FILE vetoes as `record-unreadable`, never `no-record`: in `decide` (checked before `no-record`), and through the real `agent_state.read_record` on a worker thread. The cases are a record plus one value nested 200,000 deep (its control reaps), not JSON, not UTF-8, not an object, and a directory at the record's path. The R10 re-read vetoes it too, and with one deep record and a second, healthy session, the healthy one still reaps.
  - It is pure, and ready for the mutation-sweep method.
- **Session-file reader** (`sessions/claude.py::read_session_files`, **new**; its unit test is **new**).
  - The fixture is a committed, redacted file with a dummy `name`.
  - Every missing field or wrong type makes the file unusable.
  - A pid that does not match the file name makes the file unusable.
  - JSON nested past the parser's depth makes the file unusable, never a raise. It is pinned at the parse, at the reader, and through the real reader in gather, where the same file without the deep value reaps.
  - Each unusable file is reported by its pid in `unusable`; a stale file, a name that is not an ASCII-digit pid, and a file gone before its read are not.
  - Through the real reader in gather and in the R10 re-read, one pane tree `pwsh -> claude 1000 -> claude 1001`: 1001's file readable is `ambiguous-agent`, and so is 1001's file nested too deep, missing `kind`, naming another pid, not UTF-8, or a directory. With no file for 1001 the pane reaps, and so it does with an unusable file whose pid is not in the tree.
  - An unusable file is logged once across repeated sweeps, and again after it has parsed once in between.
  - An unknown `status` reads as not quiet.
  - The stale-file rule (win32, real processes the test spawns):
    - a file for a child, carrying the child's real creation time, is returned;
    - the same file with any other `procStart` is dropped;
    - a file whose pid has exited is dropped.

    Off Windows, every file is dropped.
  - A `.key` file in the directory is never opened. The fixture's `.key` is a directory, so reading it would raise.
  - `name` never appears in the result.
- **Transcript activity.** `last_activity` is tested against tmp trees whose mtimes are set with `os.utime`. The cases: the main transcript only, a newer subagent transcript, and a missing main transcript giving None.
- **Draft detection.** `fleet.input_draft` is tested against captured pane fixtures:
  - an empty input line;
  - a draft;
  - a draft that reads "1. fix" (the draft text, not a menu option);
  - a multi-line draft under an empty caret line (a draft), and a box with no closing rule (`None`);
  - a draft whose last line is a lone caret (a draft), a rule in the transcript above the box (the box opens at the nearest rule), a box with no top rule, and a top rule not followed by the caret line (both `None`);
  - a rule typed or pasted into the draft, in three shapes: a rule then a lone caret line, an empty first line then a rule, and a pasted box fragment. Each is draft text, because the box's edges are the rules as wide as the pane's last rule. A rule exactly that wide typed into the draft, above another rule, reads `None`, not a short or an empty draft. A narrower or a wider rule in the transcript sets no width;
  - a permission dialog with the highlight on the first option, and on the last (both `None`: a menu has no input line);
  - no caret at all (`None`);
  - a busy pane;
  - `""`.
- **Kill primitive** (`tests/unit/test_procs.py::TestTerminateVerified`, **new**, win32 only). It uses real throwaway children the test spawns (`python -c "import time; time.sleep(60)"`), following the precedent of `TestRaisePriorityAboveNormal`:
  - the identity is read correctly;
  - a wrong create time means no kill;
  - the right identity means a kill, and the call returns the process's private bytes (more than 0);
  - an exited pid returns None;
  - a chain of shell stand-in → agent → grandchild loses the agent and the grandchild, deepest first, while the shell stand-in lives.

  Off Windows, `process_identity` returns None. That case runs everywhere.
- **Seam refactor.** The revive branch's `idle_sessions` tests run unchanged. There are new tests for `pane_trees`, for the `images` argument over injected snapshots, and for `agent_image_names(tools)`.
- **Console-membership stage.**
  - **`idle_sessions`, with `console_clients` faked.** The pane is not idle when:
    - a client is outside the pane's subtree;
    - the result is `None`;
    - a client is an agent or launcher image.

    It is idle when every client is in the subtree. There is exactly one helper call per `idle_sessions` call, and none when no pane survived the tree stages.
  - **`procs.console_clients`, with real processes (win32).**
    - A child the test starts on its own hidden console, with a grandchild sharing it, reads as exactly those two pids.
    - A detached child with no console reads `None`.
    - An exited pid reads `None`.
    - A tiny timeout kills the helper and reads `None` for every pid.
    - The test's own console is never swapped.
- **Orchestration** (`sweep_once` with psmux, procs and the probe faked at their seams). The pins:
  - the cap of 3, counting the sessions tried: one that R10 spares and one whose park fails each use a slot, and the sessions past the cap are never re-read;
  - oldest first;
  - the failed set, keyed by identity;
  - R10 runs before `_park` is called: a signal flipped between the first read and the re-check means no kill, and so does a record `ts` the re-read cannot read, or a second session configured and live on the same directory by the re-read (R3 `shared-cwd`);
  - a log line that carries `record_ts` formats it with `%s`, so an unknown (`None`) time logs as what it is and never raises. `quiet_s` orders the sessions and is logged as the idle age, and it is never a gate: every veto comes from a row of the decision rule;
  - the order of actions: kill, confirm, re-walk, reset, record, log;
  - the recorded `sessionId` is what gets written and logged, even when the session file is gone after the kill;
  - the park line carries every field of step 8;
  - no record and no reset when the agent survives;
  - a park with no reset when the re-walk is unknown, finds an agent, or fails the console check.
- **Serve thread.** `_supervise_idle_reap` is tested with a fake sweep:
  - the first sweep waits one interval;
  - `LockHeld` skips the sweep;
  - an exception is logged and the loop goes on;
  - an env of 0 returns at once;
  - a non-interactive logon session returns at once;
  - the stop event ends the thread;
  - `run_server` starts the thread and stops it.
- **Resume.**
  - `revive_sessions(resume_parked=True)` types `cmd /c claude --resume <id>`, never `--continue`, and clears the record.
  - With the default, a parked pane gets nothing typed into it.
  - `_revive_session` passes True.
  - `launch.revive_psmux`'s bulk callers pass nothing.
- **Config and env.**
  - Parse and serialize round-trip.
  - The unknown-key warning.
  - The floor.
  - The example-config drift pin and the `.env.example` drift pin.
  - An invalid env turns reaping off.
- **State contract.** These are the changes listed under "Compatibility with v1 writers and readers":
  - `VALID_STATES` gains `parked`, and `RECORD_VERSION == 2`;
  - a parked record has the v1 keys and types;
  - a v1 record still reads unchanged;
  - a v1 write replaces `parked`;
  - `state_hook` never writes `parked`.
  - Readers: the `watch` and `status` labels, `attention` urgency, and no title badge.
- **Isolation law.**
  - Under conftest, `reap.off_reason(default config, a platform that would allow it)` reads "off (MAGENT_IDLE_REAP=0)", and `sweep_once` returns without reading a single pane. This pins what the guard does, not that it exists.
  - A unit test scans every module under `tests/` and fails when a module sets `MAGENT_PSMUX_BOOST` but never sets `MAGENT_IDLE_REAP`. `test_psmux_boost.py` is exempt because it is about the boost itself. The scan cannot tell 0 from 1. The e2e tier's own ordering (next bullet) is what keeps its children at 0.
- **Real-multiplexer tier** (`tests/e2e/test_reap_real.py`, **new**, marker `e2e`, rides the `end-to-end` job on all 3 OSes).
  - **Gate.** On CI a missing multiplexer fails, as in `test_fleet_real.py`. Off CI the tier also needs `MDTEST_REAP_REAL=1`, because it terminates processes and a developer's box with a multiplexer usually has a live fleet on it.
  - **Fixture.** On Windows it brings sessions up through the product's own `psmux.bring_up`. The tmp config names the tool `claude` and overrides its command with the stand-in shim plus `--continue`, so `build_resume_command` exercises the real Claude rewrite. That gives shape A (a shell pane with the agent typed in). The shim-as-pane-command shortcut of `test_fleet_real.py` would not do, because there the pane would die with its agent.
  - **Stand-in.** `_fleet_agent.py` gains an opt-in mode that:
    - writes its own session file under the redirected `~/.claude/sessions/`, with its real `procStart` read through `GetProcessTimes` on itself;
    - writes a transcript;
    - enters the alternate screen with mouse tracking and bracketed paste on.

    The test writes the record.
  - **Turning the reaper on.** The test sets `MAGENT_IDLE_REAP=1` in process only, and resets the env cache (see "Test-isolation law"). It does this only after `bring_up` returns, so the psmux servers and the panes it created were all born with 0. It starts no `serve` and no `attention -d`, which are the only processes that would act on 1.
  - **Kill guards.** The park test carries `@pytest.mark.live_process_table`, because the sweep's stop walks the real process table. Before the sweep it registers every process of the stand-in's tree by identity, `own_pids.add(pid, procs.process_identity(pid))`, so the guard lets the park's kills land on those processes and fails the test on any other. The veto test does not carry the marker, so a stop reached there fails the test before it reads the table.
  - **The sweep.** It is called in process as `reap.sweep_once(cfg, tools={"claude": dataclasses.replace(AGENT_TOOLS["claude"], images=("python",))}, now=time.time() + X + 60)`. **That is the injectable seam: the agent-image set and the probe come from `tools`.** Before every sweep, the test asserts that every eligible session name starts with its own stem.
  - **Windows.** The stand-in is gone. The pane pid is the same and alive, and `has-session` holds. `#{alternate_on}` is `0`. `capture-pane` shows the notice with `--resume <id>`. The record is `parked` with the id. Then `revive_sessions(..., resume_parked=True)` restarts the stand-in, and its own log shows `--resume <id>` and no `--continue`. Four veto variants each keep the stand-in alive: a draft pasted into its input line, a permission-style numbered menu the stand-in draws in place of its input line (`pane-dialog`), a fresh `subagents/*.jsonl`, and `MAGENT_IDLE_REAP=0`.
    - **Orphan variant.** The test terminates only the stand-in's `cmd` wrapper. The sweep then parks nothing (`no-agent`), and neither a bulk revive nor, once the test has written a `parked` record, `revive_sessions(..., resume_parked=True)` types anything into the pane: the stand-in's own input log gains no line.
  - **Linux and macOS.** `bring_up` is Windows-only, so the test creates its session with `new-session` directly. The sweep parks nothing, because R1's platform gate stops it (`unsupported platform (no psmux)`), and the read-only `gather` reads `tree-unknown` for the live stand-in. This is an honest gap: there is no process snapshot off Windows.
  - Teardown kills only the servers the test created.
- **Not tested, on purpose.**
  - The 300 s serve cadence end to end. The thread's wiring is covered by unit tests, and a real-time run would cost at least 5 minutes per leg.
  - A real Claude Code in CI. The real-Claude A/B test in `poc-reap2.md` is the evidence for the stop mechanism. Any future change to the stop mechanics must re-run it by hand, with the user's consent, on a private `-L` psmux server in a scratch cwd.

## Risks and open questions

**Resolved.**

- **Bulk revive leaves parked sessions alone.** The team lead confirmed it. Interactive `up` and attach's `up --json --revive` never resume a parked session. Only status `r<n>` does, by id, and a parked record with no id is never resumed with `--continue` (see "Parked state and resume").
- **Finished only.** The user decided on 2026-09-27 that a session waiting on the user is never parked. `waiting` (R6), `needs-input` (R7) and a `dialog` pane (R9) all veto. The reason is poc-reap2 experiment C: killing a turn that has not ended drops it on resume, and Claude Code adds synthetic records instead of redoing the work. poc-reap2's own risk 3 recommended the same.

**Open.**

1. **Only X of true idleness can trigger a park, and a false "active" only delays it.** These are what could keep the reaper from ever firing:
   - **`statusUpdatedAt` semantics.** Something could bump it fleet-wide. The POC saw 21 of 31 sessions share one timestamp.
   - **A fleet-wide transcript touch.** The POC saw one about 26 minutes before its snapshot.

   If either repeats more often than X, the reaper never fires. `reap.log` will show it as a reason that never changes.
2. **The session file is undocumented.** A Claude Code update can rename a field or change a status value. The reaper then goes inert, which is safe, and logs once per file per episode.
3. **Placeholder text in an empty input box reads as a draft.** It vetoes, which is safe, but a session in that state is never parked. The veto log will show whether this happens.
4. **A reply that asks a question reads as a dialog.** `classify_state` reads `dialog` from phrases anywhere on the screen ("do you want", "press enter to", a caret followed by a digit). So a finished reply that ends "Do you want me to ...?" vetoes as `pane-dialog`, even though R6 and R7 say the turn ended. This errs toward not parking, which is safe, but it can keep a common kind of finished session from ever being parked. `reap.log` shows it as a `pane-dialog` reason that never changes. If it fires often, the follow-up is a narrower dialog test for R9 only: a dialog only when no input line exists because the caret lines are menu options. `classify_state` itself stays as it is, since `send`/`peek` depend on it.
5. **Some sessions are never parked in v1:**
   - multi-window projects (R3);
   - a session nobody has prompted since launch, which has no transcript (R8);
   - an agent orphaned from its pane tree (R5 `no-agent`). The console check also keeps every keystroke out of its pane.
6. **Orphaned children survive a park.** How much memory they hold is measured in `reap.log`, not assumed.
7. **User-wired SessionEnd hooks do not run for a reaped session.** A hard kill runs no hooks (poc-reap2). magent writes its own `parked` record; other hooks miss the event.
8. **In-process teammates (team-lead sessions) and background work are covered only indirectly.** The Stop hook's `background_tasks` ledger keeps the record at `working` (R7), and subagent transcripts veto (R8). Work that writes neither is invisible.
9. **A parked record can age out or be replaced.**
   - `sweep_stale`'s TTL (`settings.attention.stateTtlDays`, 14 days) deletes it.
   - Another writer for the same directory replaces it, for example a Claude Code the user started by hand there, outside magent (see "Compatibility with v1 writers and readers").

   In both cases the pane then counts as an ordinary dead pane, and a bulk revive types the configured `--continue` command, which is what revive does for any dead pane today. With no other agent in the directory, R3 makes `--continue` very likely to open the same conversation. With another agent there, `--continue` may open that agent's conversation instead. The notice in the pane still names the exact id.
10. **Account routing** (`feat/account-routing`, not on main) moves session files and transcripts under a per-session `CLAUDE_CONFIG_DIR`. `sweep_once` takes `config_dir`, and the probe will take the same per-session map `eligible_projects(config_dirs=...)` takes. Until then a missing file is unknown, so routing can only make v1 do less.

## Out of scope

- Sessions on nodes (`feat/nodes`).
- Codex. Its `AGENT_TOOLS` entry keeps `idle_probe=None` until a Codex probe exists.
- Automatic resume: on attach, on a keypress, on Alt+V, or through bulk revive.
- The psmux server and warm-spare overhead: about 480 MB of working set per session, and 5.3 GB of commit left fleet-wide after a full reap.
- Killing orphans. The console list is used only to keep keystrokes out of a pane, never to choose what to kill.
- Reaping off Windows.
- A manual `magent reap` command or a dry-run preview.
- Memory reporting beyond the park log line's estimate: in `status`, in `doctor`, or as a fleet total.
- Cleaning Claude Code's leftover files: stale session files, `.key` files, `.in_use` markers.
- A title badge or status-bar indicator for parked sessions.
- Parking one window of a multi-window project. That would need a store keyed by psmux session, not by cwd.
- Panes owned by another logon session.

**Docs the implementation updates:**

- README: what the feature does and how to turn it off.
- The `magent docs` config reference: `idleReap`.
- DESIGN.md §2: a new section, "Idle agents are parked, not killed", and a `reap` row in the log-writer table.
- CLAUDE.md: the `reap` log name, the `MAGENT_IDLE_REAP` isolation law, and the `test_reap_real.py` tier line.
- `.env.example` and `magent.config.example.json`.
