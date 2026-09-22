"""The launch checklist: which projects a `--go` (or the menu's launch row)
actually brings up.

`--go` used to mean "every enabled project", which is the right default and the
wrong only-option: a fleet grows, and most launches want four of its fourteen
windows. So the launch path now asks -- once, on a real terminal, with
everything already checked, so pressing Enter is byte-for-byte the old
behaviour.

Three properties are load-bearing:

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
thing across the whole product.

**Selection leaves as project NAMES**, and the launch phase applies them
(``launch.RunOpts.only``). The prompt lives in ``cli/`` and the filtering lives
in ``launch._select_projects``, which stays the pure data phase it is.

ASCII only in every glyph -- these rows render on legacy Windows code pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import click

from magent.cli import picker
from magent.cli.ui import _banner, _divider
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

# Only the first nine rows get a digit shortcut -- a two-digit address would
# need a commit key, and this list is a checklist, not a numeric entry field.
_MAX_DIGIT_ROW = 9

HINT = (
    "space toggle   a all   n none   g section   up/down move   enter launch   q cancel"
)

ABORT_MESSAGE = "Nothing launched."


@dataclass(frozen=True)
class ChecklistItem:
    """One row: the project's display name and the section it sits in."""

    name: str
    group: str = UNGROUPED


@dataclass(frozen=True)
class ChecklistResult:
    """``LAUNCH`` plus the checked names in row order, or ``ABORT``."""

    kind: str
    names: tuple[str, ...] = ()


@dataclass
class ChecklistState:
    """The whole checklist as a function of keystrokes -- no terminal involved."""

    items: list[ChecklistItem]
    cursor: int = 0
    checked: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        # Everything starts checked, because Enter has to keep meaning what
        # `--go` has always meant. A test wanting an empty start clears
        # `checked` on the constructed state.
        if not self.checked:
            self.checked = set(range(len(self.items)))

    def section(self, index: int) -> list[int]:
        """Row indices sharing ``index``'s group. Rows are ordered so a section
        is contiguous, but membership is read from the group, not the span."""
        if not 0 <= index < len(self.items):
            return []
        group = self.items[index].group
        return [i for i, item in enumerate(self.items) if item.group == group]

    def names(self) -> tuple[str, ...]:
        """The checked names, in row order -- never in set order, which is not
        one."""
        return tuple(
            item.name for i, item in enumerate(self.items) if i in self.checked
        )

    def _toggle(self, index: int) -> None:
        if index in self.checked:
            self.checked.discard(index)
        else:
            self.checked.add(index)

    def _move(self, step: int) -> None:
        if self.items:
            self.cursor = (self.cursor + step) % len(self.items)

    def press(self, key: str) -> ChecklistResult | None:
        """Apply one key. Returns a result when the checklist is finished, else
        None (keep looping and repaint)."""
        if key == picker.ENTER:
            chosen = self.names()
            # Enter with nothing checked is not an empty launch -- it is the
            # same "never mind" q means, and says so with the same line.
            return ChecklistResult(LAUNCH if chosen else ABORT, chosen)
        if key in (picker.ESC, "q", "Q"):
            return ChecklistResult(ABORT)
        if key in (picker.UP, "k"):
            self._move(-1)
            return None
        if key in (picker.DOWN, "j"):
            self._move(1)
            return None
        if key == " ":
            self._toggle(self.cursor)
            return None
        if key in ("a", "A"):
            self.checked = set(range(len(self.items)))
            return None
        if key in ("n", "N"):
            self.checked.clear()
            return None
        if key in ("g", "G"):
            rows = self.section(self.cursor)
            if all(i in self.checked for i in rows):
                self.checked.difference_update(rows)
            else:
                self.checked.update(rows)
            return None
        if key.isdigit() and key != "0":
            index = int(key) - 1
            if index < len(self.items):
                self.cursor = index
                self._toggle(index)
        return None


def _checklist_header() -> None:
    _banner()
    click.echo(f"  {style('Launch which projects?', bold=True)}")
    _divider()


def paint(state: ChecklistState) -> None:
    """Repaint the whole checklist. A full repaint per keystroke, like the
    picker: ConPTY and every POSIX terminal agree on what that means."""
    click.clear()
    _checklist_header()
    group: str | None = None
    for i, item in enumerate(state.items):
        if item.group != group:
            click.echo()
            click.echo(f"  {style(item.group, fg='cyan', dim=True)}")
            group = item.group
        mark = style(">", fg="green", bold=True) if i == state.cursor else " "
        num = f"{i + 1:>2}" if i < _MAX_DIGIT_ROW else "  "
        box = (
            style("[x]", fg="green", bold=True)
            if i in state.checked
            else style("[ ]", dim=True)
        )
        label = style(item.name, bold=True) if i == state.cursor else item.name
        click.echo(f"  {mark} {style(num, dim=True)}  {box} {label}")
    click.echo()
    click.echo(
        f"  {style(f'{len(state.checked)} of {len(state.items)} selected', dim=True)}"
    )
    click.echo(f"  {style(HINT, dim=True)}")


def run(state: ChecklistState) -> ChecklistResult:
    """Drive the checklist until the user commits. Callers must have checked
    ``picker.raw_mode_available()`` first."""
    # One key session for the whole run, entered BEFORE the first paint -- see
    # `picker.key_session` for why a per-keystroke toggle loses keys on macOS.
    with picker.key_session():
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
