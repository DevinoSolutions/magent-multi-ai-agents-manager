"""The launch checklist: which projects a `--go` (or the menu's launch row)
actually brings up.

`--go` used to mean "every enabled project", which is the right default and the
wrong only-option: a fleet grows, and most launches want four of its fourteen
windows. So the launch path now asks -- once, on a real terminal, with
everything already checked, so pressing Enter is byte-for-byte the old
behaviour.

Four properties are load-bearing:

**The prompt is a terminal-only affordance.** The gate is
``picker.raw_mode_available()`` -- the same ``sys.stdin.isatty()`` question
every other interactive list in the CLI asks. Off a terminal (a script, cron,
``CliRunner``, CI) there is no checklist and no prompt at all: the launch
narrows to nothing and behaves exactly as it did. ``--all`` is the same escape
hatch for someone who IS on a terminal and wants the whole fleet without a
keystroke.

**The state machine owns no terminal.** ``ChecklistState`` is a pure function of
keystrokes, like ``picker.PickerState``, so every transition is unit-testable
without a pty. The raw-key reading is not re-implemented here either -- it is
``picker.key_session`` and ``picker.read_key``, so navigation keys mean one
thing across the whole product; and so is the ranking: typing narrows the list
with ``picker.rank``, never a second matcher.

**Letters filter, so commands are not letters.** A fleet of fifty projects is
reached by typing a name, which means every printable key belongs to the query.
Space toggles, Enter launches, Ctrl+A checks the visible rows, Tab walks
sections, Esc clears the query (and, on an empty one, cancels).

**The frame always fits.** Each repaint is sized to the terminal: pinned title,
filter and footer, a scrolling list between them that keeps the cursor row on
screen, and ``N more`` counters where rows are cut off. It is rewritten in
place (cursor home + overwrite), never scrolled.

**Selection leaves as project NAMES**, and the launch phase applies them
(``launch.RunOpts.only``). The prompt lives in ``cli/`` and the filtering lives
in ``launch._select_projects``, which stays the pure data phase it is.

ASCII only in every glyph -- these rows render on legacy Windows code pages.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import click

from magent.cli import picker
from magent.style import style
from magent.titles import get_leaf_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from magent.config import MagentConfig, ProjectConfig

# The section header a project with no `group` lands under. A plain word, not a
# placeholder glyph: it is a heading a human reads, and the menu's group list
# has no name of its own for "ungrouped".
UNGROUPED = "other"

LAUNCH = "launch"
ABORT = "abort"

ABORT_MESSAGE = "Nothing launched."

# Lines of chrome around the list: title, filter, the two scroll counters and
# the two footer lines. The list gets whatever the terminal has left.
_CHROME_LINES = 6
_MIN_BODY = 3
# Used when the terminal cannot say how big it is.
_DEFAULT_SIZE = (80, 24)


@dataclass(frozen=True)
class ChecklistItem:
    """One row: the project's display name and the section it sits in."""

    name: str
    group: str = UNGROUPED


@dataclass(frozen=True)
class ChecklistResult:
    """``LAUNCH`` plus the chosen names in row order, or ``ABORT``."""

    kind: str
    names: tuple[str, ...] = ()


def _is_query_char(key: str) -> bool:
    """The characters a project name is typed with: letters, digits, ``-_.``.
    Space is the toggle key, so it can never be part of a query."""
    return len(key) == 1 and (key.isalnum() or key in "-_.")


@dataclass
class ChecklistState:
    """The whole checklist as a function of keystrokes -- no terminal involved.

    ``cursor`` is a position in ``visible()``, not an item index: with an empty
    query the two coincide, and under a filter the list is the ranked matches.
    ``checked`` holds ITEM indices, so a selection survives every change of
    filter.
    """

    items: list[ChecklistItem]
    cursor: int = 0
    checked: set[int] = field(default_factory=set)
    query: str = ""
    # First visible LINE of the viewport (a line is a row or a section header)
    # and how many rows PgUp/PgDn move. `frame` sets both from the terminal's
    # size; the defaults are only what a unit test sees.
    top: int = 0
    page: int = 10
    # True until the user changes the selection by hand. While it is, the
    # untouched "everything checked" default is what Enter launches -- and the
    # first typed filter character wipes it, because "type a name, press Enter"
    # must launch that project, not the whole fleet hiding behind the filter.
    pristine: bool = True

    def __post_init__(self) -> None:
        # Everything starts checked, because Enter has to keep meaning what
        # `--go` has always meant. A test wanting an empty start clears
        # `checked` on the constructed state.
        if not self.checked:
            self.checked = set(range(len(self.items)))

    @property
    def filtering(self) -> bool:
        return bool(self.query)

    def visible(self) -> list[int]:
        """Item indices on screen, in display order: the list as configured,
        or -- under a filter -- the picker's ranking of it."""
        if not self.filtering:
            return list(range(len(self.items)))
        return picker.rank(self.query, [item.name for item in self.items])

    def highlighted(self) -> int | None:
        """The item index under the cursor, or None when nothing matches."""
        vis = self.visible()
        if not vis:
            return None
        return vis[min(self.cursor, len(vis) - 1)]

    def group_starts(self) -> list[int]:
        """Cursor positions that begin a section. Empty under a filter, where
        the list is ranked across sections and has no headers."""
        if self.filtering:
            return []
        starts: list[int] = []
        group: str | None = None
        for pos, item in enumerate(self.items):
            if item.group != group:
                starts.append(pos)
                group = item.group
        return starts

    def names(self) -> tuple[str, ...]:
        """The checked names, in row order -- never in set order, which is not
        one."""
        return tuple(
            item.name for i, item in enumerate(self.items) if i in self.checked
        )

    def launch_names(self) -> tuple[str, ...]:
        """What Enter launches: the checked set, or -- when nothing is checked
        -- just the highlighted project."""
        chosen = self.names()
        if chosen:
            return chosen
        idx = self.highlighted()
        return () if idx is None else (self.items[idx].name,)

    def _retype(self, query: str) -> None:
        self.query = query
        self.cursor = 0
        self.top = 0

    def _move_to(self, pos: int) -> None:
        count = len(self.visible())
        if count:
            self.cursor = max(0, min(pos, count - 1))

    def _step(self, step: int) -> None:
        count = len(self.visible())
        if count:
            self.cursor = (min(self.cursor, count - 1) + step) % count

    def _jump_group(self, forward: bool) -> None:
        starts = self.group_starts()
        if not starts:
            return
        if forward:
            later = [p for p in starts if p > self.cursor]
            self.cursor = later[0] if later else starts[0]
        else:
            earlier = [p for p in starts if p < self.cursor]
            self.cursor = earlier[-1] if earlier else starts[-1]

    def _toggle_highlighted(self) -> None:
        idx = self.highlighted()
        if idx is None:
            return
        self.pristine = False
        if idx in self.checked:
            self.checked.discard(idx)
        else:
            self.checked.add(idx)

    def _toggle_visible(self) -> None:
        """Ctrl+A: check every VISIBLE row; if they all are already, uncheck
        them. Rows the filter hides are never touched."""
        rows = set(self.visible())
        if not rows:
            return
        self.pristine = False
        if rows <= self.checked:
            self.checked.difference_update(rows)
        else:
            self.checked.update(rows)

    def press(self, key: str) -> ChecklistResult | None:
        """Apply one key. Returns a result when the checklist is finished, else
        None (keep looping and repaint)."""
        if key == picker.ENTER:
            chosen = self.launch_names()
            # Nothing checked AND nothing matching: there is nothing to launch,
            # and nothing to abort either -- the user is still typing.
            return ChecklistResult(LAUNCH, chosen) if chosen else None
        if key == picker.ESC:
            if self.query:
                self._retype("")
                return None
            return ChecklistResult(ABORT)
        if key == picker.BACKSPACE:
            if self.query:
                self._retype(self.query[:-1])
        elif key == picker.UP:
            self._step(-1)
        elif key == picker.DOWN:
            self._step(1)
        elif key == picker.PGUP:
            self._move_to(self.cursor - self.page)
        elif key == picker.PGDN:
            self._move_to(self.cursor + self.page)
        elif key == picker.HOME:
            self._move_to(0)
        elif key == picker.END:
            self._move_to(len(self.visible()) - 1)
        elif key == picker.TAB:
            self._jump_group(True)
        elif key == picker.BTAB:
            self._jump_group(False)
        elif key == picker.CTRL_A:
            self._toggle_visible()
        elif key == " ":
            self._toggle_highlighted()
        elif _is_query_char(key):
            if self.pristine:
                self.checked.clear()
                self.pristine = False
            self._retype(self.query + key)
        return None


# -- viewport -----------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One line of the list: a section header (``pos`` is None, ``text`` is the
    section) or the row at cursor position ``pos``."""

    pos: int | None
    text: str = ""


def list_lines(state: ChecklistState) -> list[Line]:
    """The whole list as lines, headers included. Headers appear only in the
    unfiltered view: a ranked list crosses sections, and a header over it would
    claim rows belong to a section they do not."""
    lines: list[Line] = []
    group: str | None = None
    for pos, idx in enumerate(state.visible()):
        item = state.items[idx]
        if not state.filtering and item.group != group:
            lines.append(Line(None, item.group))
            group = item.group
        lines.append(Line(pos))
    return lines


def scroll_top(lines: Sequence[Line], cursor: int, height: int, top: int) -> int:
    """The viewport's first line, moved as little as possible to keep the
    cursor's row on screen. A section's first row also pulls its header in, so
    a Tab jump lands under the heading it jumped to."""
    at = next((n for n, line in enumerate(lines) if line.pos == cursor), 0)
    want = at - 1 if at > 0 and lines[at - 1].pos is None else at
    top = min(top, want)
    if at >= top + height:
        top = at - height + 1
    return max(0, min(top, max(0, len(lines) - height)))


def hidden_rows(lines: Sequence[Line], top: int, height: int) -> tuple[int, int]:
    """Rows (not headers) scrolled off above and below the viewport."""
    above = sum(1 for line in lines[:top] if line.pos is not None)
    below = sum(1 for line in lines[top + height :] if line.pos is not None)
    return above, below


def body_height(rows: int) -> int:
    """Lines available to the list in a terminal ``rows`` tall. One row stays
    free so the last line never ends in a newline that scrolls."""
    return max(_MIN_BODY, rows - 1 - _CHROME_LINES)


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


def frame(state: ChecklistState, rows: int, cols: int) -> list[str]:
    """Every line of one repaint, sized to a ``rows`` x ``cols`` terminal. Also
    settles the viewport (``state.top``) and the PgUp/PgDn page size, so the
    cursor is always inside what this frame shows."""
    height = body_height(rows)
    state.page = max(1, height - 1)
    lines = list_lines(state)
    vis = state.visible()
    state.cursor = min(state.cursor, max(0, len(vis) - 1))
    state.top = scroll_top(lines, state.cursor, height, state.top)
    above, below = hidden_rows(lines, state.top, height)
    width = max(20, cols - 1)

    out = [f"  {style('Launch which projects?', bold=True)}"]
    shown_query = state.query[-(width - 12) :]  # the tail, where typing happens
    out.append(f"  filter: {style(shown_query, bold=True)}{style('_', dim=True)}")
    out.append(f"  {style(f'^ {above} more', dim=True)}" if above else "")
    shown = lines[state.top : state.top + height]
    for line in shown:
        if line.pos is None:
            out.append(f"  {style(_fit(line.text, width - 2), fg='cyan', dim=True)}")
            continue
        idx = vis[line.pos]
        item = state.items[idx]
        on = line.pos == state.cursor
        mark = style(">", fg="green", bold=True) if on else " "
        box = (
            style("[x]", fg="green", bold=True)
            if idx in state.checked
            else style("[ ]", dim=True)
        )
        label = _fit(item.name, width - 10)
        label = style(label, bold=True) if on else label
        group = style(f"  {_fit(item.group, 18)}", dim=True) if state.filtering else ""
        out.append(f"  {mark} {box} {label}{group}")
    if not vis:
        out.append(
            f"   {style('no match for', dim=True)} {style(state.query, bold=True)}"
        )
    out.extend([""] * (height - len(shown) - (0 if vis else 1)))
    out.append(f"  {style(f'v {below} more', dim=True)}" if below else "")
    out.extend(_footer(state, width))
    return out


def _footer(state: ChecklistState, width: int) -> list[str]:
    status = f"{len(state.checked)} of {len(state.items)} selected"
    if state.filtering:
        status += f" - {len(state.visible())} shown"
    else:
        status += " - type to filter"
    launch = "enter launch"
    if not state.checked:
        idx = state.highlighted()
        if idx is not None:
            launch += f" {state.items[idx].name}"
    keys = ["space toggle", launch, "ctrl+a all"]
    if state.group_starts():
        keys.append("tab next group")
    keys.append("esc clear" if state.query else "esc cancel")
    return [
        f"  {style(_fit(status, width - 2), dim=True)}",
        f"  {style(_fit('  '.join(keys), width - 2), dim=True)}",
    ]


def terminal_size() -> tuple[int, int]:
    """``(rows, columns)`` of the terminal right now."""
    size = shutil.get_terminal_size(_DEFAULT_SIZE)
    return size.lines, size.columns


# Home the cursor, rewrite each line in place clearing its old tail, then clear
# whatever a taller previous frame left below. No newline after the last line:
# that is the one that would scroll the terminal.
_HOME = "\x1b[H"
_EOL = "\x1b[K"
_CLEAR_BELOW = "\x1b[J"


def paint(state: ChecklistState) -> None:
    """Repaint the checklist in place, sized to the terminal as it is NOW (so a
    resize heals on the next keystroke)."""
    rows, cols = terminal_size()
    body = f"{_EOL}\n".join(frame(state, rows, cols))
    click.echo(f"{_HOME}{body}{_EOL}{_CLEAR_BELOW}", nl=False)


def run(state: ChecklistState) -> ChecklistResult:
    """Drive the checklist until the user commits. Callers must have checked
    ``picker.raw_mode_available()`` first."""
    # One key session for the whole run, entered BEFORE the first paint -- see
    # `picker.key_session` for why a per-keystroke toggle loses keys on macOS.
    with picker.key_session():
        click.clear()
        while True:
            paint(state)
            result = state.press(picker.read_key())
            if result is not None:
                click.echo()
                return result


def _in_group(project: ProjectConfig, group: str | None) -> bool:
    if not group:
        return True
    return bool(project.group) and project.group.lower() == group.lower()


def items_for(projects: Sequence[ProjectConfig]) -> list[ChecklistItem]:
    """Rows in display order: groups in the order the config first mentions
    them, ungrouped projects last under ``UNGROUPED``.

    The name is the one every other magent surface shows -- the project's
    ``title`` or its path's leaf -- so what you check here is what the launch
    row, the window title and the psmux session are all called.
    """
    groups: list[str] = []
    for project in projects:
        group = project.group or UNGROUPED
        if group not in groups:
            groups.append(group)
    groups.sort(key=lambda g: g == UNGROUPED)  # stable: ungrouped section last
    return [
        ChecklistItem(project.title or get_leaf_name(project.path), group)
        for group in groups
        for project in projects
        if (project.group or UNGROUPED) == group
    ]


@dataclass(frozen=True)
class Choice:
    """What the launch path needs back. ``only`` is ``RunOpts.only``: a set of
    project names, or None for "everything", which is what a skipped checklist
    means. ``aborted`` is the user saying never mind."""

    only: frozenset[str] | None = None
    aborted: bool = False


def choose_projects(config: MagentConfig, *, group: str | None = None) -> Choice:
    """Ask which of ``config``'s enabled projects to launch.

    Returns the untouched ``Choice()`` -- launch everything, no narrowing, no
    prompt -- off a terminal or when the group narrows to nothing (that case
    already has its own message, printed by ``launch._select_projects``).
    """
    if not picker.raw_mode_available():
        return Choice()
    candidates = [p for p in config.projects if p.enabled and _in_group(p, group)]
    if not candidates:
        return Choice()
    result = run(ChecklistState(items_for(candidates)))
    if result.kind == ABORT:
        return Choice(aborted=True)
    return Choice(only=frozenset(result.names))
