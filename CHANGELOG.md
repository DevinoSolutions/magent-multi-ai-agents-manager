# Changelog

All notable changes to magent are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.16.0] - 2026-08-31

### Added

- **Clipboard captures are PNG now -- ~10-20x smaller than the BMPs they
  replace.** An Alt+V screenshot used to land as a ~1.7 MB uncompressed
  BMP under `~/.magent/uploads`; the capture now encodes the mainstream
  clipboard shapes (uncompressed 24/32bpp DIBs, standard channel masks)
  as PNG with a pure-stdlib encoder, and the upload names the file and
  its MIME type from the actual bytes. Anything the encoder does not
  positively recognize still ships as the proven BMP wrap -- a wrong
  image is worse than a big one, so unrecognized shapes are refused,
  never guessed at.

### Fixed

- **psmux window names belong to magent, not to whatever the pane is
  running.** psmux's automatic rename showed the pane's current command
  in the status line -- after a Claude Code self-update that read
  `0:claude.exe.old` instead of anything about your project. Every
  status-line decoration pass now pins the window name to the project
  name and switches automatic renaming off, so existing sessions heal on
  their next decoration tick -- the same ownership doctrine the window
  *titles* already follow.

## [3.15.1] - 2026-08-31

### Fixed

- **Alt+V went silently dead on local machines in 3.15.0 -- native local
  paste is now opt-in.** 3.15.0 made a local press deliver one injected
  Ctrl+V instead of running the capture/upload pipeline, but Claude Code
  on Windows reacts only to a *physical* Ctrl+V and ignores the injected
  key -- so the press reported success while nothing pasted. The default
  is back to the proven capture/upload path everywhere; set
  `MAGENT_ALTV_NATIVE=1` to opt in to native delivery for panes whose
  agent demonstrably pastes on an injected Ctrl+V. (psmux delivers the
  key correctly -- the same injection pastes fine in a PowerShell pane;
  the agent's input stack is the boundary.)
- **Pressing F2 no longer pops a console window full of VS Code logs.**
  The hotkey listener is a console-less process, so the `code.cmd` shim
  it spawned was allocated a brand-new visible console that streamed VS
  Code's internal logs for as long as the editor ran. The spawn is now
  windowless with detached streams -- same family as 3.14.0's
  empty-terminal-storm fix, on the one spawn it missed.

## [3.15.0] - 2026-08-31

### Changed

- **A local Alt+V press pastes natively -- the capture/upload pipeline
  is for machines apart.** The BMP capture, HTTP upload and `send-keys`
  inject exist to move an image between machines (laptop viewer to
  desktop host over attach; phone to host over the upload page). On the
  same machine they are pure overhead: the pane's agent reads the
  clipboard itself on a paste keystroke, and locally that clipboard is
  the one you copied into. A press whose listener is locally wired now
  sends exactly one Ctrl+V into the pane and the agent pastes a real
  attachment -- no capture, no upload, no file under
  `~/.magent/uploads`. Remote-attach and phone uploads are untouched,
  and `MAGENT_ALTV_NATIVE=0` restores the upload path locally for
  agents without native clipboard paste. The status line narrates the
  shorter script: `Alt+V: pasting...` then `Alt+V: pasted from
  clipboard`.

### Added

- **A real end-to-end guard against the empty-terminal storm.** A new
  cross-OS test tier drives a genuinely console-less `magent serve`
  through flash, upload-inject and session-probe traffic and asserts
  no psmux spawn ever opens a visible console window -- the regression
  behind 3.14.0's burst-of-empty-terminals fix can no longer return
  silently.

## [3.14.0] - 2026-08-31

### Added

- **Type-to-filter project picker in the interactive menu and session
  switcher.** Start typing a project name and the list narrows and
  reorders to the closest matches -- prefix beats word-boundary beats
  substring beats in-order subsequence, ties keep config order -- with
  arrow keys to move the highlight and Enter to launch the top match.
  Digit shortcuts, single-key commands (`q`, `d`, ...) and the
  bare-Enter default are byte-for-byte unchanged, and non-tty callers
  keep the line-based prompt through the same renderer. One shared
  component (`cli/picker.py`) drives the menu, the group submenu and
  the session switcher; a real-pseudo-terminal test tier pins the
  interactive path on Windows, macOS and Linux.
- **The same typeahead on the mobile upload page.** The project pills
  gain a filter box with the identical ranking rules as the CLI;
  typing narrows and reorders, arrows walk the highlight, and Enter
  and a tap share one selection path. Pinned by real-Chromium
  browser tests.
- **`magent terminal install` / `magent terminal status`.** Installs
  Windows Terminal `sendInput` keybindings that survive psmux, which
  drops key modifiers in transit: Ctrl+Backspace becomes the Ctrl+W
  word-erase byte and Shift+Enter becomes ESC CR (what Claude Code's
  `/terminal-setup` writes -- and that tool refuses to run inside a
  tmux/psmux pane, which is why magent ships this). The engine
  round-trips the whole settings file and refuses -- never rewrites --
  one the stdlib parser can't read (Windows Terminal accepts JSONC),
  writes control characters as escape text, supports both settings
  schema generations, never overrides a user's existing binding, and
  backs the file up before any write. `magent doctor` gains a
  matching `wt-keys` check, warn-at-worst.
- **The psmux fleet runs at above-normal priority, kept there by a
  sweep.** Typing into a magent window under heavy fleet CPU load
  used to lag badly: the multiplexer's client and server are
  windowless normal-priority processes Windows never boosts, so a
  saturated box starved the interactive path. Three owners now hold
  every psmux process at AboveNormal -- the launch bring-up, the
  attention daemon's poll tick, and a `magent serve` supervisor
  thread -- admin-free, idempotent, and never downgrading a process
  someone set higher. `MAGENT_PSMUX_BOOST=0` opts out. CI pins
  psmux 3.3.8, which carries the matching upstream fix.
- **`magent attention -d` now supervises the upload server.** The
  daemon probes the configured port every tick and respawns a dead
  `magent serve` (rate-limited by a 60-second cooldown), so a crashed
  server heals without anyone noticing the phone uploads went dark.
  `magent status`/`doctor` name the death instead of hiding it.
  Gated on `settings.uploadServer` and `MAGENT_UPLOAD_SUPERVISOR=0`.
- **`magent doctor` diagnoses a machine-wide psmux control-plane
  wedge** -- the state where every psmux command hangs and nothing
  else on the box says why.

### Fixed

- **Alt+V no longer spawns a burst of empty terminal windows.** On
  Windows, a console-subsystem child of a console-less parent is
  given a brand-new console, and Windows 11's default-terminal
  setting materializes each one as a real, empty Windows Terminal
  window. The background fleet (`magent serve`, `attention -d`, the
  hotkey listener) runs console-less, so every one-shot psmux client
  it launched popped a window -- one press (three narration flashes,
  the paste injection, and a discovery fan-out probing every
  configured session at once) opened dozens of empty terminals,
  froze the desktop's terminals, and only then landed the paste.
  Every psmux control/probe spawn now carries `CREATE_NO_WINDOW`,
  pinned by a contract test that fails the gate on any future spawn
  without it.
- **The picker no longer drops keys typed between keystrokes on
  macOS.** The interactive picker used to toggle the terminal into
  raw mode around every read; macOS discards input queued during
  that toggle (Linux preserves it), so a fast typist or a paste
  could lose characters. One cbreak session now spans the whole
  pick loop. The real-pty test tier also learned that a pty child
  is only as alive as its reader: waits that poll the filesystem
  now keep draining the terminal, which unblocked a deterministic
  macOS CI failure.
- **Concurrent magent processes no longer lose log records to
  rotation.** Several processes share one log name; the stdlib
  rotating handler renames the live file to rotate, which fails on
  Windows under contention and then silently drops every subsequent
  record while the file grows unbounded (measured: 272 of 800 records
  lost). The shared handler now holds no file across records and
  serializes each one under a cross-process lock. A multi-process
  end-to-end test pins every-record-present-exactly-once on all
  three OSes.
- **The phone no longer calls a slow paste a failed upload.** The
  mobile page reads all three paste states -- pasted, still pending,
  refused -- and shows a stalled-but-saved upload in the healthy
  tint with honest wording instead of a false failure.
- **`magent serve` defaults its port to the config's `uploadPort`**
  instead of a hard-coded literal.
- Three test-suite flakes burned down root-cause-first (one was a
  product bug), and the suite is now provably isolated from the
  developer's real `~/.magent` -- a leaking test once stopped a
  live machine's Alt+V listener with a green suite.

## [3.13.1] - 2026-08-18

### Fixed

- **The upload reply is no longer hostage to the paste.** Sending a
  snip used to mean waiting out the paste into the agent's pane inside
  the upload request itself: when psmux was slow to accept `send-keys`
  (measured at 74 seconds under load), the Alt+V listener's own
  20-second deadline expired first and narrated `upload failed - is
  magent serve running?` for an upload that had in fact landed on disk
  and would eventually paste. The paste now runs on its own worker with
  exactly one attempt (bounded at 60 seconds -- a retry against a
  slow-but-live psmux double-pastes), every `psmux send-keys` call is
  bounded instead of hanging forever, and the upload reply waits at
  most 3 seconds before answering with one of three honest paste
  states: pasted, still pasting, or refused. A stalled paste now
  narrates `Alt+V: image saved - psmux is slow, paste still pending`
  -- a success tint, because the image is safe on disk -- instead of a
  false failure. Measured press-to-outcome against a 30-second psmux
  stall: 3.1 seconds. A new non-mocked end-to-end test drives that
  exact stall through a real `magent serve` and pins the fast answer,
  the pending narration, the byte-identical file, and the
  single-attempt guarantee on every OS.
- **The Alt+V end-to-end tier no longer loses its own evidence on
  Windows.** The tier's recording multiplexer appended all records to
  one shared file, and Windows appends are not atomic -- concurrent
  invocations tore or dropped lines, failing roughly two of every
  three Windows CI runs with phantom "missing flash" and "missing
  paste" verdicts. The product itself never lost a flash (the flash
  pipeline is serialized end-to-end; that is now proven and pinned).
  Each recorder invocation writes its own atomically-published record
  file, a torn record is a loud failure instead of a silent skip, and
  a regression test pins zero loss under twelve concurrent spawns.

## [3.13.0] - 2026-08-18

### Added

- **Every Alt+V press narrates itself on the status line, from the chord.**
  Pressing Alt+V used to give no feedback until the whole upload finished
  -- and under load, often no feedback at all, because the status-line
  flash was killed by its own 3-second subprocess timeout before the bar
  could repaint. The press pipeline now flashes three phases into the
  project's status line: `Alt+V: capturing...` the moment the chord is
  recognized (before the clipboard is even read), `Alt+V: uploading...`
  once the image is in hand, and then the outcome -- success included.
  Failures name their cause specifically (`clipboard has no image - copy
  one first`, `cannot reach magent serve (connection refused)`,
  `serve said HTTP 400: <reason>`, `saved, but psmux would not paste it`)
  instead of a generic error. Flashes are dispatched through one FIFO
  pump, so a press never waits on its own progress report and the phases
  can never arrive out of order; the serve no longer adds a second voice
  to a press the listener already narrates. Measured press-to-bar latency:
  65-176 ms. A new non-mocked end-to-end tier drives a real press against
  a real `magent serve` and a real recorded multiplexer on every OS, so
  the phase order, the failure texts, and the latency budget are pinned
  against regression.

### Fixed

- **The reconnect status line no longer erases what you were typing.**
  When a connection dropped mid-session, the in-place "reconnecting"
  line was drawn wherever the cursor happened to sit -- which, in an
  agent session, is usually inside the prompt box, on top of the sentence
  you had typed but not yet sent. The supervisor now paints the status
  line on the terminal's bottom row with an absolute jump-and-return
  (save cursor, draw, restore), so the frozen frame -- your typed text
  included -- stays exactly where it was through the whole outage and is
  still there after the link heals. Keystrokes made during the outage are
  forwarded, not swallowed. A real-terminal test tier replays the actual
  byte stream into a screen model and asserts the grid: only the bottom
  row may change. This runs on the *attaching* machine, so the client
  side must upgrade to see it.

- **A silent or chatty child can no longer hang the end-to-end suite
  until CI cancels the job.** The pseudo-terminal test driver had two
  deadline holes (an untimed read on Windows, and a deadline check
  skipped whenever output kept arriving) that let one blocked test burn
  the whole job's time budget and take every other result with it. The
  driver now enforces a wall-clock deadline on every wait and carries the
  partial transcript in the failure, and every pty test runs under a
  whole-test time budget. Exposed along the way: driving a Windows
  pseudo-terminal *over* Windows OpenSSH nests two ConPTYs and mangles
  Enter into a raw key record the remote shell never accepts -- that leg
  is now a loud skip on Windows (it still runs for real on Linux and
  macOS) and a ledger entry.

## [3.12.3] - 2026-08-18

### Fixed

- **An outage is a status line, not a log.** During a connection drop, an
  attach pane used to print a fresh multi-line block for every redial
  attempt -- plus ssh's own raw noise (`Connection timed out`,
  `client_loop: send disconnect`) -- so a long wifi flap scrolled the pane
  full of junk that then wrapped into overlapping garbage. The reconnect
  supervisor now renders the whole outage as **one line updated in
  place**: target, attempt count, a live retry countdown, ssh's last
  complaint condensed, and the Ctrl+C hint -- clipped to the pane width so
  it can never wrap (narrow panes drop the hint first, then the target,
  never the countdown). ssh's stderr is captured off-screen during redial;
  host-key-changed warnings still pass straight through, and if the pane
  gives up, ssh's last lines are printed so the cause is never hidden.
  When the link heals, the status line is erased and the session takes
  back over cleanly, with a one-line "reconnected after N attempts"
  record kept in scrollback. Panes with redirected output keep plain
  one-line-per-attempt logging. Auth prompts are unaffected -- ssh asks
  for passwords via the terminal directly, not stderr. The reconnect
  decision logic (redial on 255, out-of-band session probe, bounded
  give-up) is byte-for-byte unchanged; this runs on the *attaching*
  machine, so the client side must upgrade to see it.

## [3.12.2] - 2026-08-18

### Fixed

- **Launching from inside an AI agent no longer poisons the spawned
  sessions.** Running `magent up` from a shell hosted by a coding agent
  (Claude Code, a CI harness) leaked that harness's environment into every
  spawned session: inherited session markers made the child agent believe
  it was a nested sub-session -- silently disabling transcript saving --
  and an inherited `NO_COLOR` rendered every pane in plain white. Every
  spawn that hosts an agent (psmux sessions, launch-path terminals, VS Code
  windows, the F2 editor) now routes through one scrubbing seam that strips
  the launcher's agent-session markers, its colour overrides
  (`NO_COLOR`/`FORCE_COLOR`/`CLICOLOR`/`CLICOLOR_FORCE`), and the
  multiplexer nesting markers. The scrub is an exact list, not a namespace
  sweep -- credentials and user configuration pass through untouched, and
  `TERM` is never modified.

- **`magent down` now stops every session it promised, verifies the kills,
  and reports only what it proved.** Three defects let sessions survive a
  `down --all` while the report claimed success: session liveness had three
  independent implementations with different retry policies, so under load
  `down` could see fewer live sessions than the picker did and silently
  skip the rest; `down --all` acted on that one unretried probe instead of
  the configured session list; and the summary counted kill *attempts*,
  never re-checking reality. Liveness is now one shared seam (probe with
  one retry), `down` targets the configured sessions themselves, kills in a
  bounded parallel fan-out (no more sequential sweep a remote 60s timeout
  could truncate to a config-order tail -- the remote budget is now 300s),
  then re-probes and re-kills survivors -- and the report names, in red,
  any session that would not stop instead of counting it as stopped.

## [3.12.1] - 2026-08-18

### Fixed

- **A flaky connection no longer closes attach panes -- they redial until
  the host answers.** The reconnect supervisor trusted the ssh exit code to
  tell a deliberate detach (exit 0, stop) from a connection failure (255,
  reconnect). But a Windows host never propagates a remote command's exit
  status over a pty: a session that *died* also handed the pane a clean 0,
  so during a wifi flap every pane closed announcing a detach the user never
  made. On any exit other than 255 the supervisor now asks the host over a
  separate non-pty connection -- where exit codes are truthful on every
  OS -- whether the session still exists: alive means a real detach (the
  pane stops as before); gone or unanswerable means the pane keeps
  redialling, bounded so a session that is genuinely never coming back
  stops with the cause named after five looks. Only a positive "the session
  is alive" answer can ever close a pane. The probe asks psmux, not magent,
  so it works against older hosts; the attach command itself is
  byte-identical, so corpse detection and `--no-reconnect` behave exactly
  as before.

- **Session creation now breaks out of the launching process's job.** psmux
  servers were started with a plain spawn, inheriting whatever Windows Job
  Object the launcher sat in -- so a session's lifetime could in principle
  be coupled to the process tree that created it (an ssh connection, a
  terminal about to close). Servers are now spawned through the same
  breakaway path the upload server already used. Hardening: CI could not
  reproduce a session dying with its connection even without this change,
  so it closes a documented gap rather than a demonstrated one.

- **`magent down --all` now says what it does.** Its help and the README
  state plainly that it stops every psmux session *and the agent running
  inside each* -- not just the background daemons.

## [3.12.0] - 2026-08-16

### Added

- **`magent serve` now owns the Alt+V listener -- and magent says when
  Alt+V is broken.** The hotkey listener used to be spawned once by whatever
  launched it and then forgotten: a reboot, a crash, or an upgrade left
  Alt+V silently dead, with `magent status` showing a healthy system. The
  upload server now supervises the listener -- it starts one if none is
  running, re-checks every 30 seconds, and restarts it if it dies -- so "the
  upload server is up" and "Alt+V works" are the same fact. A live listener
  aimed at a remote host by `magent attach` is left exactly as aimed; the
  supervisor only ever fills an empty slot. `MAGENT_HOTKEY_SUPERVISOR=0`
  opts out.

  The health is now visible instead of guessed: `magent status` reports the
  listener three-state -- `ON`, a red `DEAD (upload server is up but no
  listener -- Alt+V does nothing)` that also sets exit code 3 and prints the
  repair command, or an honest `off (starts with the upload server)`.
  `magent doctor` gained a matching `hotkey` check. Every Alt+V press now
  writes one `ALTV outcome=<x> project=<y>` line to
  `~/.magent/logs/hotkey.log`, and every failure (no image on the clipboard,
  clipboard unreadable, upload rejected, unexpected error) flashes a
  plain-words explanation into the project's psmux status line -- a press
  that does nothing now always says why.

### Fixed

- **Window titles stay magent's, even when the app inside rewrites them.**
  A terminal tab renamed by the agent running in it (or by the shell)
  dropped out of the `magent:` title grammar -- removing that window from
  tiling, attach dedupe, corpse pairing, and the Alt+V project lookup all at
  once. The attention daemon now remembers magent windows *by OS handle* and
  repairs a stomped title on its next tick, and a new lint rule (MD006)
  makes it impossible to add a Windows Terminal spawn that forgets
  `--suppressApplicationTitle`. Linux terminals that support locking the
  title get the equivalent flag at spawn time (alacritty, xterm; kitty's is
  already permanent).

- **Re-tile now tiles what is actually on screen -- attach panes included.**
  `magent --retile-all` (and menu option 2) built its window list from the
  local config's projects, so the windows `magent attach` opens -- whose
  names are the *remote* host's session names, present in no local config --
  were never re-tiled, while configured projects whose windows were closed
  were enqueued anyway and sat through a retry deadline before a red "not
  found". Retile now snapshots the screen and tiles exactly the magent-owned
  windows that are open right now: configured windows first (in config
  order), then every other window carrying a `magent:` title, badge-proof
  and deduped by name. Closed windows are skipped outright, and nothing is
  ever spawned. `magent --go --retile-all` keeps its combined meaning --
  launch whatever is missing, then tile everything, attach panes included.

## [3.11.1] - 2026-08-14

### Fixed

- **A brand-new project no longer opens a dead pane.** The default claude
  command carries `--continue`, which resumes the most recent conversation
  *for the current working directory* -- and in a directory that has never
  hosted one, claude exits with "No conversation found to continue": the
  agent never starts, and revive re-runs the same failing command forever.
  Every command-build site (fresh launch, session bring-up, revive, and the
  command list attach uses for `--no-mux` windows) now routes through one
  seam, `sessions.build_start_command`, which checks the project's stored
  sessions on the machine that will run the command and drops the implicit
  resume flag only when there is positively nothing to resume -- starting a
  fresh `claude` instead, with a `launch.log` line recording the decision.
  Deliberately narrow so real failures stay visible instead of being papered
  over: an explicit `--resume <id>`, a per-window `command` override, a
  remote project, a probe that errors, and present-but-unreadable session
  files all keep the configured command byte-for-byte -- if `--continue`
  then genuinely fails, the error stays on screen where it can be read.
  There is no shell-level `|| claude` fallback anywhere: it would fire on
  *any* nonzero exit (masking auth failures, corrupted sessions, CLI
  regressions), and magent never observes the command's exit code in a
  psmux pane in the first place. Codex needs no equivalent (its resume form
  is the explicit `codex resume <id>`, only built when an id exists), but a
  hand-configured `codex resume --last` gets the same guard.

## [3.11.0] - 2026-08-09

### Added

- **Attach windows now reconnect on their own after a connection loss.**
  Remote attach panes used to die with `client_loop: send disconnect` /
  `[process exited with code 255]` whenever the client machine slept or the
  network changed, leaving every terminal a corpse until the next
  `magent attach` closed and reopened them all. Each pane now runs a small
  supervisor (`magent-attach-client`) around the ssh client: when the
  connection drops it prints a reconnect countdown and redials on a backoff
  ladder (2s doubling, capped at 30s, reset after any connection that lasted
  at least 30s) until the host answers again -- panes heal themselves even
  when the host comes back hours later. A deliberate detach (F1 /
  clean exit) and Ctrl+C still end the pane without redialling, and a remote
  command that fails over a healthy connection stops with the cause named
  instead of hammering the host's sshd. `magent attach --no-reconnect`
  restores the previous one-shot behaviour, `--no-mux` panes are never
  supervised (redialling would start a second agent), and the spawn falls
  back to bare ssh with a warning if the supervisor script is not on PATH.
  The ssh dial also gained `-o ConnectTimeout=20` so a redial against a
  sleeping host fails fast instead of hanging for minutes.

  Platform note: Windows OpenSSH does not propagate a remote command's exit
  status over a pty, so on a Windows *host* a remote-command failure is
  indistinguishable from a clean detach -- both stop the pane; reconnect
  itself is unaffected because connection failures are reported by the local
  ssh client.

## [3.10.10] - 2026-08-08

### Fixed

- **Bringing sessions up from inside a magent window no longer creates
  nothing.** psmux (like tmux) exports `PSMUX_SESSION`,
  `PSMUX_TARGET_SESSION`, `TMUX`, `TMUX_PANE` and friends into every pane it
  owns, and magent is routinely driven from inside one of its own windows --
  the interactive menu's `u` especially. The `new-session` child then
  inherited those markers and hit the nested-session guard: `psmux: sessions
  should be nested with care, unset PSMUX_SESSION to force`, after which psmux
  3.3.6 exits 0 having created nothing at all. magent's sessions are SIBLINGS
  by construction -- one session per socket, never a session inside a session
  -- so that one child now runs with the markers stripped
  (`env.psmux_child_env`, the one module allowed to touch `os.environ`).
  Scoped to session CREATION deliberately: measured against the real binary
  from inside a live pane, control and probe commands (`has-session -t`,
  `display-message -t`, `capture-pane -t`) return the same exit code and the
  same bytes whether the markers are present or stripped -- the guard fires
  for `new-session` alone -- so they keep plain inheritance instead of a
  rebuilt environment block under every psmux round-trip. Exactly the
  in-a-session markers go: `TMUX`, `TMUX_PANE` and the `PSMUX*` family.
  `TMUX_TMPDIR` (and any future `*_TMPDIR`) is kept -- it names the directory
  the server's sockets live in, so a child that loses it looks in the default
  location, finds nothing, and reports every session dead. The user-facing
  *attach* client keeps the inherited environment on purpose: attaching from
  inside a pane really is nesting, and psmux's warning is right to fire there.
- **Liveness probes tell the truth.** `psmux -L <name> has-session` with no
  `-t` exits 0 even when no server exists on that socket (proven live:
  `psmux -L definitely-not-a-session-xyz has-session` -> rc 0; psmux also
  keeps internal `__warm__` spare servers per socket that answer it). Every
  probe in the product was therefore blind -- `magent status` reported 42 of
  42 sessions up where the truthful probe found 40 up and 2 down, the menu
  said "All N session(s) already running", the bring-up creation verify could
  never detect a casualty, and revive and the corpse sweeps had nothing to
  act on. All three probe sites (`psmux.has_session`, `psmux_status`'s
  fan-out, the picker's liveness sweep) and the launch path's dedupe check now
  pass `-t <session>`. Safe by construction: magent runs one session per
  socket and the session name is the socket name, so `-t`'s prefix matching
  has nothing else to match.
- **One session psmux refuses no longer aborts the whole bring-up with a
  traceback.** A `new-session` that exited non-zero (`psmux: failed to create
  session 'X'`) raised `CalledProcessError` out of `launch_psmux_session`, and
  `launch_verified`'s first launch call was the one call in that function not
  wrapped -- so `magent up` and the menu's `u` ended in a traceback (the
  reported "error towards the end") with every remaining session in the wave
  abandoned. A refused window is now logged and skipped, the rest of its batch
  and every later batch still run, and the creation verify -- which already
  knows how to respawn what is missing -- is what decides the outcome.
- **A bring-up no longer reports casualties as successes.** `psmux.bring_up`
  discarded the creation verify's answer and returned every name it had
  attempted, so `magent up` and the menu printed "Brought up N session(s)"
  for sessions the same run had logged as `never came up after respawn; left
  down`. It now returns `(created, failed)`, and both interactive paths (plus
  the `--go` launch path) print a red `x N session(s) failed to come up:
  <names>` line naming them. `up --json` is unchanged: it is a pure read that
  never brings anything up, so it has no casualty set to report -- a session
  that failed shows up in its existing `down` list.

## [3.10.9] - 2026-08-05

### Fixed

- **`magent attach` no longer waits minutes on a cosmetic status bar.**
  `magent up --json` (the host command attach runs over SSH for session
  status) refreshed every live session's F1/F2 status-line hints
  synchronously: five psmux control commands per session, run serially
  under a 3-second timeout each. On a busy host -- 41 agent sessions all
  working -- those commands time out rather than answer, so decorating
  cost ~15s per session and ~45s for the pass, all of it before a byte of
  JSON was printed. The attach client's 30s status timeout fired
  mid-pass, and its retry (120s) re-ran the whole host command from
  scratch, so an attach where every session was ALREADY up still took
  minutes and re-entered the revive path concurrently with itself.
  Evidence from a real run: 518 `status-line decoration failed ... timed
  out after 3 seconds` lines in 92 seconds of `launch.log`. Decoration
  has always been cosmetic and best-effort, so the status path now fires
  it and returns: all commands go out at once as detached processes
  (every stdio handle at `DEVNULL`, nothing waited on), the same shape
  the launch path already used for a fresh batch. A stamp file
  (`~/.magent/state/decor.stamp`, 60s TTL) keeps attach's repeated
  bring-up polls from piling up processes against a psmux server that is
  already struggling. Sessions created during a bring-up are unaffected:
  they are still decorated directly at birth.

### Added

- **F2 now says something in viewers that can't honour it.** The status
  bar advertises `F2 </> VS Code`, but F2 is handled by the Windows
  magent hotkey listener -- so in Termius, a phone SSH app, or a plain
  `ssh` from another machine the key fell through to the pane and did
  nothing, silently, while the hint still promised it. Sessions whose F2
  hint is advertised now also carry `bind -n F2 display-message` on their
  own psmux server, explaining that F2 opens VS Code only from a magent
  window on Windows. It cannot double-fire where the listener IS running:
  the low-level hook swallows F2 before the terminal ever sees it, so the
  message reaches exactly the viewers that were getting nothing. Hosts
  with no `code` on PATH (which never advertise the F2 half) now send
  `unbind-key -n F2` instead, so a stale binding can't outlive the hint.

## [3.10.8] - 2026-08-04

### Fixed

- **A session that never comes up during a bring-up storm is now detected
  and respawned.** A big attach (or `--go`) launching dozens of sessions
  at once can wedge an individual psmux server hard enough that its
  session never materializes -- and nothing noticed: the send-keys
  verifier (3.10.6) deliberately leaves unreadable panes alone, so a
  session that never EXISTED was invisible to it, and the project simply
  showed "down" in the picker until the next `magent attach` happened to
  recreate it. Bring-up now proves every session it launched answers
  `has-session` (bounded concurrent probe after a short settle), respawns
  the missing ones exactly once through the full original launch path --
  send-keys verification and decoration included -- and re-probes;
  still-missing sessions are named in an error log and left alone, so one
  stuck session can never cost the wave. The retry posture is
  deliberately the opposite of the send verifier's: re-typing into an
  unreadable pane could stomp a live agent, but re-running `new-session`
  is harmless, so here an unknown (including a probe timeout against a
  wedged server) counts as missing and gets the retry. Both bring-up
  entry points share the verify -- attach storms and `--go` launches.
  Greppable telemetry in `launch.log`: `session did not come up after
  bring-up; respawning: <names>` / `session never came up after respawn;
  left down: <names>`.

## [3.10.7] - 2026-08-03

### Fixed

- **`done` now means done -- not "the orchestrator stopped talking".** The
  `magent-state-hook` writer mapped Claude Code's `Stop` event straight to
  `done`, but `Stop` fires when the main agent's TURN ends -- an agent
  that just dispatched background subagents (or long background shell
  commands) ends its turn in seconds and goes quiet while that work
  grinds on, so sessions running multi-minute background jobs were badged
  `done` almost immediately. The Stop payload carries Claude Code's own
  pending-work ledger (`background_tasks`, verified against the live
  CLI); the hook now consults it -- a Stop with live entries writes
  `working`, and only a Stop with a drained ledger writes `done`. The
  harness re-invokes the session when background work completes, so the
  final drained Stop arrives on its own. Unknown ledger shapes count as
  still-running (premature `done` misleads; a late one self-corrects);
  Claude Code versions predating the ledger keep the old behavior. No
  hook re-wiring needed -- open sessions pick the fix up on upgrade.

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

[3.14.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.13.1...v3.14.0
[3.13.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.13.0...v3.13.1
[3.13.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.12.3...v3.13.0
[3.12.3]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.12.2...v3.12.3
[3.12.2]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.12.1...v3.12.2
[3.12.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.12.0...v3.12.1
[3.12.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.11.1...v3.12.0
[3.11.1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.11.0...v3.11.1
[3.11.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.10...v3.11.0
[3.10.10]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.9...v3.10.10
[3.10.9]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.8...v3.10.9
[3.10.8]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.7...v3.10.8
[3.10.7]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/v3.10.6...v3.10.7
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
