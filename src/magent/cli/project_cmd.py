"""`magent new` / `magent remove` -- start a project in its own folder, and take
one back out of magent.

The point of `new` is a clean way to keep each agent chat in a folder of its
own that magent already tracks: it makes `<baseDir>/<name>`, writes the
project entry, `git init`s the folder and (at a terminal, on a yes) brings up
just that one project. `remove` is the discoverable other half and never
touches a file: it edits the config, and first stops the project's running
session, because `down --all` acts on CONFIGURED sessions and a removed
project's session would otherwise be orphaned with nothing left to stop it.

Both commands are thin shells over two functions that RETURN DATA or raise
`ProjectError` (`create_project`, `remove_project`); the shells own the exit
codes, the `--json` envelopes and the prompts' wording. The interactive menu's
`n`/`r` rows and `magent config remove` call the same functions, so the three
entry points cannot answer differently.

Every refusal is one specific line, exit 1, and happens BEFORE anything is
created or written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click

from magent.cli.app import main
from magent.cli.config_io import (
    _as_dict,
    _as_str,
    _load_config_or_exit,
    _load_raw_config,
    _project_dicts,
    _save_raw_config,
    _sublist,
    _validate_config_text,
)
from magent.config import default_config
from magent.paths import find_config
from magent.style import style
from magent.titles import get_leaf_name

# Names Windows refuses as a file or folder, with or without an extension
# ("CON.txt" is as reserved as "CON").
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
_GIT_TIMEOUT_S = 30


class ProjectError(Exception):
    """A refusal: said in one line, exit 1, nothing created or written."""


@dataclass(frozen=True)
class Created:
    name: str  # what the project goes by (its title, else the folder name)
    folder: str  # the absolute folder, as a forward-slash string
    git: bool  # a repository was initialized in it
    git_note: str  # why not, when ``git`` is False and one is worth saying
    base_dir_saved: str | None  # the baseDir this call chose, else None
    config_created: bool


@dataclass(frozen=True)
class Removed:
    query: str
    names: list[str]
    folder: str  # where the (first) project's folder is; never touched
    stopped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _can_prompt() -> bool:
    """Is a person at this console? The product's one answer
    (`console.human_at_console`: on Windows a bare ``isatty`` is True for NUL,
    so a script would "ask" a question nobody can answer). A seam so a test can
    answer it."""
    from magent import console

    return console.human_at_console()


def _slash(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def _expand(raw: str) -> str:
    return os.path.expandvars(os.path.expanduser(raw))


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# --- names -----------------------------------------------------------------


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a project folder, in one line -- or None."""
    if not name.strip():
        return "a project name cannot be empty"
    if "/" in name or "\\" in name:
        return (
            f"'{name}' is a path, not a name -- use --in <dir> to choose the "
            "parent folder"
        )
    if ".." in name:
        return f"'{name}' cannot contain '..'"
    bad = sorted({c for c in name if c in _INVALID_NAME_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(repr(c)[1:-1] for c in bad)
        return f"'{name}' contains characters a folder name cannot have: {shown}"
    if name != name.strip() or name.endswith("."):
        return f"'{name}' cannot start or end with a space, or end with a dot"
    if name.split(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
        return f"'{name}' is a reserved device name on Windows"
    return None


# --- new -------------------------------------------------------------------


def _project_folder(data: dict[str, object], entry: dict[str, object]) -> str:
    """The absolute folder a configured project's ``path`` names."""
    raw = _expand(_as_str(entry.get("path")))
    if Path(raw).is_absolute():
        return raw
    base = _expand(_as_str(data.get("baseDir")))
    return os.path.join(base, raw) if base else raw


def _already_configured(data: dict[str, object], name: str, target: Path) -> str | None:
    for entry in _project_dicts(data):
        leaf = get_leaf_name(_as_str(entry.get("path")))
        title = _as_str(entry.get("title"))
        if name.casefold() in (leaf.casefold(), title.casefold()) or _same_path(
            _project_folder(data, entry), target
        ):
            return f"'{name}' is already a project ({_as_str(entry.get('path'))})"
    return None


def _choose_base_dir(data: dict[str, object], interactive: bool) -> str:
    """The configured baseDir; when unset, ask once at a terminal."""
    base = _as_str(data.get("baseDir"))
    if base:
        return base
    if not interactive:
        raise ProjectError(
            "no projects folder is set -- pass --in <dir> or run "
            "`magent config base-dir <dir>`"
        )
    default = _slash(Path.home() / "projects")
    answer = click.prompt(
        f"  {style('Where should new projects live?', bold=True)}",
        default=default,
        prompt_suffix=" ",
    ).strip()
    if not answer:
        raise ProjectError("no projects folder chosen -- nothing created")
    return _slash(Path(_expand(answer)).resolve())


def _git_init(folder: Path) -> tuple[bool, str]:
    """``git init`` in FOLDER: ``(done, why-not)``. No commit, no remote. A
    missing git is a dim note, never a failure."""
    git = shutil.which("git")
    if not git:
        return False, "git not found on PATH -- skipped git init"
    try:
        proc = subprocess.run(
            [git, "init", "--quiet"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git init did not run ({exc})"
    if proc.returncode != 0:
        return False, f"git init failed: {(proc.stderr or '').strip()[:120]}"
    return True, ""


def create_project(
    config_file: Path,
    name: str,
    *,
    group: str | None = None,
    tool: str | None = None,
    color: str | None = None,
    title: str | None = None,
    parent: str | None = None,
    use_git: bool = True,
    interactive: bool = False,
) -> Created:
    """Make ``<parent or baseDir>/<name>``, add it to the config, ``git init``
    it. Raises ProjectError before creating or writing anything."""
    problem = name_problem(name)
    if problem:
        raise ProjectError(problem)

    config_created = not config_file.exists()
    data = default_config([]) if config_created else _load_raw_config(config_file)

    chosen_base: str | None = None
    if parent:
        parent_dir = Path(_expand(parent)).resolve()
        stored_base = _as_str(data.get("baseDir"))
        in_base = bool(stored_base) and _same_path(parent_dir, _expand(stored_base))
        stored_path = name if in_base else _slash(parent_dir / name)
    else:
        base = _choose_base_dir(data, interactive)
        if not _as_str(data.get("baseDir")):
            chosen_base = base
        parent_dir = Path(_expand(base))
        stored_path = name
    target = parent_dir / name

    clash = _already_configured(data, title or name, target)
    if clash:
        raise ProjectError(clash)
    if target.exists() and not target.is_dir():
        raise ProjectError(f"{_slash(target)} exists and is not a folder")
    if target.is_dir() and any(target.iterdir()):
        raise ProjectError(f"{_slash(target)} already exists and is not empty")

    entry: dict[str, object] = {"path": stored_path}
    for key, value in (
        ("group", group),
        ("tool", tool),
        ("color", color),
        ("title", title),
    ):
        if value:
            entry[key] = value
    # Color left unset on purpose: load_config backfills a deterministic one,
    # the same palette `config add` projects get.
    _sublist(data, "projects").append(entry)
    if chosen_base:
        data["baseDir"] = chosen_base
    why = _validate_config_text(json.dumps(data))
    if why is not None:
        raise ProjectError(f"not saved: {why}")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectError(f"could not create {_slash(target)}: {exc}") from exc
    git_done, git_note = _git_init(target) if use_git else (False, "")
    _save_raw_config(config_file, data)
    return Created(
        name=title or name,
        folder=_slash(target.resolve()),
        git=git_done,
        git_note=git_note,
        base_dir_saved=chosen_base,
        config_created=config_created,
    )


def _open_only(config_file: Path, name: str) -> int:
    """Bring up ONLY ``name`` through the normal launch path -- the project
    checklist's own selection (``RunOpts.only``). No fleet-wide re-tile."""
    from magent.launch import (  # heavy subsystem: in-body per policy
        RunOpts,
        run_magent,
    )

    cfg = _load_config_or_exit(config_file)
    return run_magent(
        cfg,
        RunOpts(
            retile_all=False,
            config_path=str(config_file),
            only=frozenset({name}),
        ),
    )


def _report_created(made: Created) -> None:
    if made.config_created:
        click.echo(f"  {style('+', fg='green')} Created a new config.")
    if made.base_dir_saved:
        click.echo(
            f"  {style('+', fg='green')} Projects folder set to {made.base_dir_saved}"
        )
    click.echo(
        f"  {style('+', fg='green')} Created {style(made.name, fg='cyan', bold=True)}"
        f"  {style(made.folder, dim=True)}"
    )
    if made.git:
        click.echo(f"  {style('+', fg='green')} Initialized a git repository.")
    elif made.git_note:
        click.echo(f"  {style('-', dim=True)} {style(made.git_note, dim=True)}")


def run_new(
    config_file: Path,
    name: str,
    *,
    group: str | None = None,
    tool: str | None = None,
    color: str | None = None,
    title: str | None = None,
    parent: str | None = None,
    use_git: bool = True,
    open_now: bool | None = None,
) -> int | None:
    """Create a project and say so; then open it (``open_now`` True), skip
    (False), or ask at a terminal (None). Returns the launch's exit code when
    it launched, else None. Raises ProjectError on a refusal."""
    interactive = _can_prompt()
    made = create_project(
        config_file,
        name,
        group=group,
        tool=tool,
        color=color,
        title=title,
        parent=parent,
        use_git=use_git,
        interactive=interactive,
    )
    _report_created(made)
    if open_now is None and interactive:
        open_now = click.confirm("  Open it now?", default=True)
    if open_now:
        return _open_only(config_file, made.name)
    click.echo(
        f"  {style('Next:', dim=True)} magent --go"
        f"  {style(f'(tick {made.name} in the checklist)', dim=True)}"
    )
    return None


@main.command("new")
@click.argument("name")
@click.option("--group", "-g", default=None, help="Group name")
@click.option("--tool", "-t", default=None, help="Tool (claude, codex, vscode, ...)")
@click.option("--color", "-c", default=None, help="Tab color (#rrggbb)")
@click.option("--title", default=None, help="Custom window title")
@click.option(
    "--in",
    "parent",
    default=None,
    metavar="DIR",
    help="Create it under DIR instead of the projects folder (stores an absolute path)",
)
@click.option("--no-git", is_flag=True, help="Do not run `git init` in the folder")
@click.option(
    "--open/--no-open",
    "open_now",
    default=None,
    help="Bring the project up now, or not (default: ask at a terminal)",
)
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON")
@click.pass_context
def new_cmd(
    ctx: click.Context,
    name: str,
    group: str | None,
    tool: str | None,
    color: str | None,
    title: str | None,
    parent: str | None,
    no_git: bool,
    open_now: bool | None,
    as_json: bool,
) -> None:
    """Start a new project in its own folder, tracked by magent.

    Makes <projects folder>/NAME, adds it to the config, runs `git init` in it
    and (at a terminal) offers to open it. Where the projects folder is: the
    config's baseDir -- asked once, at a terminal, when it is not set yet.
    Exit 0 when created, 1 when refused (nothing is created or written then).
    """
    if as_json and open_now:
        raise click.UsageError("--json cannot be combined with --open")
    config_file = find_config(ctx.obj.get("config_path"))
    try:
        if as_json:
            made = create_project(
                config_file,
                name,
                group=group,
                tool=tool,
                color=color,
                title=title,
                parent=parent,
                use_git=not no_git,
                interactive=False,
            )
            click.echo(
                json.dumps(
                    {
                        "ok": True,
                        "name": made.name,
                        "path": made.folder,
                        "git": made.git,
                    }
                )
            )
            return
        rc = run_new(
            config_file,
            name,
            group=group,
            tool=tool,
            color=color,
            title=title,
            parent=parent,
            use_git=not no_git,
            open_now=open_now,
        )
    except ProjectError as exc:
        _refuse(str(exc), as_json)
    if rc:
        sys.exit(rc)


def _refuse(message: str, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps({"ok": False, "error": message}))
    else:
        click.echo(f"  {style('x', fg='red')} {message}", err=True)
    sys.exit(1)


# --- remove ----------------------------------------------------------------


def _project_name(entry: dict[str, object]) -> str:
    return _as_str(entry.get("title")) or get_leaf_name(_as_str(entry.get("path")))


def _match_projects(
    projects: list[dict[str, object]], query: str, *, loose: bool
) -> list[dict[str, object]]:
    """The entries ``query`` names. Always: an exact path, folder name or
    title (all entries sharing it -- one project listed twice). With ``loose``
    (``magent remove``): then a case-insensitive exact name, then the fleet's
    one resolver (unique substring, else unique prefix). A loose match that
    is not unique raises, naming the candidates."""
    wanted = _slash(query)
    exact = [
        p
        for p in projects
        if wanted in (_slash(_as_str(p.get("path"))), _project_name(p))
        or get_leaf_name(_as_str(p.get("path"))) == query
    ]
    if exact or not loose:
        return exact
    from magent import fleet  # heavy subsystem: in-body per policy

    names = [_project_name(p) for p in projects]
    folded = [p for p in projects if _project_name(p).casefold() == query.casefold()]
    if folded:
        return folded
    hit = fleet.resolve_session(query, names)
    if hit is not None:
        return [p for p in projects if _project_name(p) == hit]
    near = [n for n in names if query.casefold() in n.casefold()]
    if len(set(near)) > 1:
        raise ProjectError(
            f"'{query}' is ambiguous: {', '.join(sorted(set(near)))} -- be more specific"
        )
    return []


def _is_node_project(entry: dict[str, object]) -> bool:
    return entry.get("node") not in (None, "cloud")


def _node_placed(entry: dict[str, object]) -> bool | None:
    """Is this node project's session placed on a node (what `down` would
    stop there)? None when the node map cannot be read."""
    from magent import nodes  # heavy subsystem: in-body per policy
    from magent.config import ProjectConfig

    try:
        entries = nodes.load_node_map_strict()
    except (OSError, ValueError):
        return None
    proj = ProjectConfig(
        path=_as_str(entry.get("path")), title=_as_str(entry.get("title")) or None
    )
    return nodes.placement_of(proj, entries) is not None


def _live_local(entries: list[dict[str, object]]) -> list[str]:
    """The local psmux sessions of ENTRIES that are up now: the ONE liveness
    seam (`psmux.live_sessions`), and only when psmux is installed."""
    from magent import psmux  # heavy subsystem: in-body per policy

    binary = psmux.find_psmux()
    if not binary:
        return []
    sids = list(
        dict.fromkeys(
            psmux.session_name(_project_name(e))
            for e in entries
            if not _as_str(e.get("host"))
        )
    )
    return psmux.live_sessions(sids, psmux=binary) if sids else []


def remove_project(
    config_file: Path,
    query: str,
    *,
    loose: bool = True,
    stop: bool = False,
    interactive: bool = False,
) -> Removed:
    """Take ``query``'s project out of the config, stopping its session first
    when it is running. Never deletes the folder or any file. Raises
    ProjectError (nothing written) on a refusal."""
    data = _load_raw_config(config_file)
    projects = _project_dicts(data)
    matches = _match_projects(projects, query, loose=loose)
    if not matches:
        raise ProjectError(f"No project matching '{query}' found.")
    name = _project_name(matches[0])
    folder = _slash(_project_folder(data, matches[0]))

    notes: list[str] = []
    for entry in matches:
        if _is_node_project(entry):
            placed = _node_placed(entry)
            if placed is None or placed:
                raise ProjectError(
                    f"{name} runs on a node -- stop it first with "
                    f"`magent down {name}`, then remove it"
                )
        if _as_str(entry.get("host")):
            notes.append(
                f"sessions on {_as_str(entry.get('host'))} are not touched "
                f"(`magent down {name} --host {_as_str(entry.get('host'))}`)"
            )

    stopped: list[str] = []
    live = _live_local(matches)
    if live:
        if not stop:
            if not interactive:
                raise ProjectError(
                    f"{name}'s session is running -- stop it with "
                    f"`magent remove {name} --stop`"
                )
            if not click.confirm(
                "  Its session is running. Stop it first?", default=True
            ):
                raise ProjectError(f"{name}'s session is still running -- not removed")
        from magent import psmux  # heavy subsystem: in-body per policy

        stopped, still = psmux.stop_sessions(live)
        if still:
            raise ProjectError(
                f"could not stop {', '.join(still)} -- {name} was not removed"
            )

    drop = {id(p) for p in matches}
    data["projects"] = [
        p
        for p in _sublist(data, "projects")
        if not (isinstance(p, dict) and id(p) in drop)
    ]
    _save_raw_config(config_file, data)
    return Removed(
        query=query,
        names=[_project_name(p) for p in matches],
        folder=folder,
        stopped=stopped,
        notes=notes,
    )


def report_removed(gone: Removed) -> None:
    count = len(gone.names)
    click.echo(f"  Removed {count} project(s) matching {style(gone.query, fg='cyan')}")
    for sid in gone.stopped:
        click.echo(f"  {style('+', fg='green')} Stopped {sid}")
    click.echo(
        f"  {style('-', dim=True)} {style('Folder left in place: ' + gone.folder, dim=True)}"
    )
    for note in gone.notes:
        click.echo(f"  {style('-', dim=True)} {style(note, dim=True)}")


@main.command("remove")
@click.argument("name")
@click.option(
    "--stop",
    is_flag=True,
    help="Stop its running session first, without asking",
)
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON")
@click.pass_context
def remove_cmd(ctx: click.Context, name: str, stop: bool, as_json: bool) -> None:
    """Remove a project from magent. Never deletes its folder or any file.

    NAME is the project's name, folder name or path (case-insensitive; a
    unique part of it is enough). A running session is stopped first -- asked
    at a terminal, or pass --stop -- so it is not left behind with nothing
    tracking it. Exit 0 when removed, 1 when refused (nothing changes then).
    """
    config_file = find_config(ctx.obj.get("config_path"))
    if not config_file.exists():
        _refuse(f"No config found at: {config_file}", as_json)
    try:
        gone = remove_project(
            config_file,
            name,
            loose=True,
            stop=stop,
            interactive=_can_prompt() and not as_json,
        )
    except ProjectError as exc:
        _refuse(str(exc), as_json)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "name": gone.names[0],
                    "path": gone.folder,
                    "stopped": gone.stopped,
                }
            )
        )
        return
    report_removed(gone)


# --- the interactive menu's rows -------------------------------------------


def menu_new(config_file: Path) -> int | None:
    """Menu row `n`: ask for a name, then the same flow as `magent new`.
    Returns the launch's exit code when it opened the project, else None."""
    name = click.prompt(
        f"  {style('Project name', bold=True)} {style('(blank to cancel)', dim=True)}",
        default="",
        show_default=False,
        prompt_suffix=" ",
    ).strip()
    if not name:
        return None
    try:
        return run_new(config_file, name)
    except ProjectError as exc:
        click.echo(f"  {style('x', fg='red')} {exc}", err=True)
        return None


def menu_remove(config_file: Path) -> None:
    """Menu row `r`: pick a configured project (number or name), then the
    same flow as `magent remove`."""
    if not config_file.exists():
        click.echo(f"  {style('No config yet.', dim=True)}")
        return
    names = [
        _project_name(_as_dict(p))
        for p in _project_dicts(_load_raw_config(config_file))
    ]
    if not names:
        click.echo(f"  {style('No projects to remove.', dim=True)}")
        return
    for i, n in enumerate(names, 1):
        click.echo(f"   {style(str(i).rjust(2), dim=True)}  {n}")
    answer = click.prompt(
        f"  {style('Remove which?', bold=True)} {style('(number or name, blank to cancel)', dim=True)}",
        default="",
        show_default=False,
        prompt_suffix=" ",
    ).strip()
    if not answer:
        return
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        answer = names[int(answer) - 1]
    try:
        report_removed(
            remove_project(config_file, answer, loose=True, interactive=True)
        )
    except ProjectError as exc:
        click.echo(f"  {style('x', fg='red')} {exc}", err=True)
