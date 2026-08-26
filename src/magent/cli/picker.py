"""The ONE type-to-filter list picker: fuzzy ranking, a key-driven state
machine, and the raw-key terminal loop that drives them.

Every interactive list magent asks you to choose from goes through here -- the
main menu, its group submenu, and the psmux session switcher -- so the three
cannot drift into three different ideas of what typing a project name means.

Three properties are load-bearing, in this order:

**The ranking is deterministic.** ``rank`` is pure, stdlib-only and total:
case-insensitive, prefix above word-boundary above substring above in-order
subsequence, and ties broken by the caller's original order and nothing else.
No timestamps, no frecency, no locale. A picker that reorders itself between
two identical keystrokes is a picker you cannot pin, and this one is pinned
exhaustively in ``tests/unit/test_picker.py``.

**The raw-key mode is gated on a REAL terminal.** ``raw_mode_available``
answers the same ``sys.stdin.isatty()`` question the rest of the CLI already
asks. Off a terminal -- a pipe, a script, Click's ``CliRunner`` -- callers keep
their existing line-based ``click.prompt`` untouched, byte for byte. That is
not politeness: the whole non-interactive surface of this product (and most of
its test suite) types lines, and a raw-mode read there would block on a
console that does not exist.

**An empty query is today's menu.** Nothing filters, nothing is highlighted,
the rows render through ``ui._menu_item`` exactly as they always have, digits
still address rows by their printed number, and a bare Enter still falls
through to the caller's default. Filter mode begins only when you type
something that is neither a row number nor one of the caller's single-key
commands (``q``, ``a``, ``n``, ...) -- so ``q`` is still Quit even when a
project called ``queue-worker`` is on screen. Arrow keys are the other entry
point: once you have moved the highlight, Enter takes what is highlighted
rather than the default.

Rendering is a full repaint per keystroke (``click.clear`` + rows), not a
cursor-arithmetic patch: ConPTY and every POSIX terminal agree on what a
repaint means, and the result stays plain text once ANSI is stripped -- which
is exactly what the real-PTY tier asserts against.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

from magent.cli.ui import _menu_item
from magent.style import style

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

# -- ranking ------------------------------------------------------------------

# Match quality, best first. Values are compared, never displayed.
PREFIX = 0
BOUNDARY = 1
SUBSTRING = 2
SUBSEQUENCE = 3
NO_MATCH = 4


def _boundary_at(text: str, pos: int) -> bool:
    """True if ``pos`` starts a word in ``text``.

    A word starts at index 0, after any non-alphanumeric character (``-``,
    ``_``, ``.``, ``/``, a space) or at a lower-to-upper camelCase seam. That
    covers every shape a project name takes in a magent config.
    """
    if pos <= 0:
        return True
    prev = text[pos - 1]
    if not prev.isalnum():
        return True
    return prev.islower() and text[pos].isupper()


def _is_subsequence(needle: str, haystack: str) -> bool:
    """True if every character of ``needle`` appears in ``haystack``, in order."""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def match_tier(query: str, candidate: str) -> int:
    """How well ``candidate`` matches ``query`` -- one of the tier constants.

    Case-insensitive. An empty query matches everything at the best tier, so
    ``rank`` degenerates to "the list, in order", which is the no-filter state.
    """
    q = query.strip().lower()
    if not q:
        return PREFIX
    low = candidate.lower()
    # Boundary detection reads the ORIGINAL text (camelCase needs the case),
    # which is only index-aligned with the lowered copy when lowering did not
    # change the length -- a handful of non-ASCII characters expand. Fall back
    # to the lowered text there rather than index into the wrong string.
    ref = candidate if len(low) == len(candidate) else low
    if low.startswith(q):
        return PREFIX
    best = NO_MATCH
    pos = low.find(q)
    while pos >= 0:
        best = min(best, BOUNDARY if _boundary_at(ref, pos) else SUBSTRING)
        if best == BOUNDARY:
            return BOUNDARY
        pos = low.find(q, pos + 1)
    if best < NO_MATCH:
        return best
    return SUBSEQUENCE if _is_subsequence(q, low) else NO_MATCH


def rank(query: str, candidates: Sequence[str]) -> list[int]:
    """Indices of the candidates matching ``query``, best match first.

    Sorted by ``(tier, original index)`` and by nothing else -- a tie is broken
    by the order the caller passed, so the same query over the same list always
    produces the same list.
    """
    if not query.strip():
        return list(range(len(candidates)))
    scored = [
        (tier, i)
        for i, c in enumerate(candidates)
        if (tier := match_tier(query, c)) < NO_MATCH
    ]
    scored.sort()
    return [i for _, i in scored]


# -- keys ---------------------------------------------------------------------

UP = "up"
DOWN = "down"
ENTER = "enter"
BACKSPACE = "backspace"
ESC = "esc"
IGNORED = ""

# The console-mode escape prefixes Windows uses for a special key: the NEXT
# read carries the key code itself.
_WIN_PREFIXES = ("\x00", "\xe0")
_WIN_SPECIAL = {"H": UP, "P": DOWN}
# ESC [ A / ESC [ B on POSIX (and anything ConPTY passes through verbatim).
_CSI_SPECIAL = {"A": UP, "B": DOWN}
# Ctrl+P / Ctrl+N: the readline-shaped aliases. They are single bytes, so they
# survive every terminal layer that might swallow or re-encode an arrow's
# escape sequence -- which is why the real-PTY tier can drive navigation on
# Windows at all.
_CTRL_ALIASES = {"\x10": UP, "\x0e": DOWN}

# How long to wait for the rest of an escape sequence before calling a lone
# ESC a lone ESC. Generous for a local terminal, invisible to a human.
_ESC_TAIL_S = 0.05


def _classify(ch: str) -> str:
    """Map one plain character to a key token (or IGNORED)."""
    if ch in ("\r", "\n"):
        return ENTER
    if ch in ("\x7f", "\x08"):
        return BACKSPACE
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in _CTRL_ALIASES:
        return _CTRL_ALIASES[ch]
    if ch == "\x1b":
        return ESC
    if ch >= " " and ch != "\x7f":
        return ch
    return IGNORED


def _read_key_windows() -> str:
    # The platform gate is the caller's; repeating it here is what lets the
    # type checker prove the win-only import away on POSIX (same shape as
    # cli/watch.py's keypress poll).
    if sys.platform != "win32":
        return IGNORED
    import msvcrt  # win-only stdlib module

    ch = msvcrt.getwch()
    if ch in _WIN_PREFIXES:
        return _WIN_SPECIAL.get(msvcrt.getwch(), IGNORED)
    if ch == "\r":
        # A pasted / programmatically-typed line arrives as CRLF, and the
        # trailing LF would otherwise land on the NEXT prompt as a second
        # Enter -- taking that prompt's default without anyone pressing a key.
        # A human's Enter is a bare CR, so nothing is waiting and nothing is
        # eaten.
        if msvcrt.kbhit():
            nxt = msvcrt.getwch()
            if nxt != "\n":
                return _pushback(nxt)
        return ENTER
    return _classify(ch)


_PENDING: list[str] = []


def _pushback(ch: str) -> str:
    """Queue a character read one too early, and answer ENTER for the CR that
    caused the over-read."""
    _PENDING.append(ch)
    return ENTER


def _read_key_posix() -> str:
    # Mirror of the guard in ``_read_key_windows``: termios/tty do not exist on
    # Windows, and this half is only ever reached off it.
    if sys.platform == "win32":
        return IGNORED
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return _classify(ch)
        if not select.select([sys.stdin], [], [], _ESC_TAIL_S)[0]:
            return ESC
        if sys.stdin.read(1) != "[":
            return IGNORED
        if not select.select([sys.stdin], [], [], _ESC_TAIL_S)[0]:
            return IGNORED
        return _CSI_SPECIAL.get(sys.stdin.read(1), IGNORED)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_key() -> str:
    """Block for one keypress and return its key token or its character.

    Raises ``KeyboardInterrupt`` on Ctrl+C: raw mode turns off the terminal's
    own signal generation, so the interrupt has to be re-created here or Ctrl+C
    silently becomes a control character nobody handles.
    """
    if _PENDING:
        return _classify(_PENDING.pop(0))
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


def raw_mode_available() -> bool:
    """True when a real terminal is on stdin AND this OS's raw-read module
    exists. Everything else -- pipes, ``CliRunner``, cron -- keeps the
    line-based prompt it has always had."""
    try:
        if not sys.stdin.isatty():
            return False
    except (OSError, ValueError):
        return False
    module = "msvcrt" if sys.platform == "win32" else "termios"
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# -- state --------------------------------------------------------------------

SELECT = "select"
TEXT = "text"
CANCEL = "cancel"


@dataclass(frozen=True)
class PickerItem:
    """One selectable row. ``key`` is what the caller gets back -- the printed
    row number, or the menu letter -- so a filtered list still addresses rows
    by the number next to them."""

    key: str
    label: str
    extra: str = ""
    key_fg: str = "cyan"
    # Reproduces the blank lines today's menus use to group their rows. Honored
    # only in the unfiltered view; a filtered list is compact by design.
    gap_before: bool = False
    # What ranking matches against, when that is not the visible label (a
    # session row's project name, say). Empty means "use the label".
    haystack: str = ""

    def target(self) -> str:
        return self.haystack or self.label


@dataclass(frozen=True)
class PickerResult:
    """``SELECT`` + the chosen item's key, ``TEXT`` + whatever was typed (the
    caller's existing branch logic owns it), or ``CANCEL``."""

    kind: str
    value: str


@dataclass
class PickerState:
    """The whole picker as a function of keystrokes -- no terminal involved, so
    the behaviour is unit-testable without a pty."""

    items: list[PickerItem]
    commands: frozenset[str] = frozenset()
    query: str = ""
    highlight: int = 0
    # Set by an arrow key. It is what makes Enter mean "take the highlighted
    # row" while the query is still empty, without disturbing the bare-Enter
    # default nobody has touched the highlight for.
    moved: bool = False

    @property
    def filtering(self) -> bool:
        """True once the query is something other than a row number or one of
        the caller's single-key commands."""
        q = self.query.strip().lower()
        return bool(q) and not q.isdigit() and q not in self.commands

    def visible(self) -> list[int]:
        """Item indices on screen, in display order."""
        if not self.filtering:
            return list(range(len(self.items)))
        return rank(self.query, [i.target() for i in self.items])

    def highlighted(self) -> int | None:
        """The item index Enter would take, or None when nothing matches."""
        vis = self.visible()
        if not vis:
            return None
        return vis[min(self.highlight, len(vis) - 1)]

    def _retype(self, query: str) -> None:
        """Any change to the query re-aims at the best match: the highlight
        goes back to the top and prior arrow movement is forgotten, so the
        row Enter takes is the one the new query recommends."""
        self.query = query
        self.highlight = 0
        self.moved = False

    def press(self, key: str) -> PickerResult | None:
        """Apply one key. Returns a result when the picker is finished, else
        None (keep looping and repaint)."""
        if key == ENTER:
            idx = self.highlighted()
            if idx is not None and (self.filtering or self.moved):
                return PickerResult(SELECT, self.items[idx].key)
            return PickerResult(TEXT, self.query.strip())
        if key == ESC:
            if self.query:
                self._retype("")
                return None
            return PickerResult(CANCEL, "")
        if key == BACKSPACE:
            self._retype(self.query[:-1])
            return None
        if key in (UP, DOWN):
            vis = self.visible()
            if vis:
                step = -1 if key == UP else 1
                self.highlight = (min(self.highlight, len(vis) - 1) + step) % len(vis)
                self.moved = True
            return None
        if len(key) == 1:
            self._retype(self.query + key)
        return None


# -- rendering ----------------------------------------------------------------

# ASCII only, deliberately: this line renders on legacy Windows code pages and
# inside psmux panes, and an ambiguous-width glyph has corrupted a magent status
# bar before.
HINT = "type to filter   up/down to move   enter to pick   esc to clear"


def paint(
    state: PickerState,
    render_header: Callable[[], None],
    *,
    hint: str = "",
    prompt: str = "",
) -> None:
    """Repaint the whole list. Unhighlighted rows go through ``ui._menu_item``,
    the renderer today's menus already use, so the unfiltered first paint is
    the one users have always seen."""
    click.clear()
    render_header()
    visible = state.visible()
    marked = state.highlighted() if (state.filtering or state.moved) else None
    for idx in visible:
        item = state.items[idx]
        if item.gap_before and not state.filtering:
            click.echo()
        if idx == marked:
            click.echo(
                f" {style('>', fg='green', bold=True)} "
                f"{style(item.key, fg=item.key_fg, bold=True)}   "
                f"{style(item.label, bold=True)}{item.extra}"
            )
        else:
            _menu_item(item.key, item.label, key_fg=item.key_fg, extra=item.extra)
    if not visible:
        click.echo(
            f"   {style('no match for', dim=True)} {style(state.query, bold=True)}"
        )
    click.echo()
    if hint:
        click.echo(f"  {style(hint, dim=True)}")
    if prompt:
        click.echo(f"{prompt}{state.query}", nl=False)


def show(items: Iterable[PickerItem], render_header: Callable[[], None]) -> None:
    """One unfiltered paint, for the line-input path. Same renderer as the raw
    loop's first frame, so the two input modes cannot show different lists."""
    paint(PickerState(list(items)), render_header)


def pick(
    items: Iterable[PickerItem],
    render_header: Callable[[], None],
    *,
    commands: Iterable[str] = (),
    prompt: str,
    hint: str = HINT,
) -> PickerResult:
    """Run the raw-key picker until the user commits. Callers must have checked
    ``raw_mode_available`` first."""
    state = PickerState(list(items), frozenset(c.lower() for c in commands))
    while True:
        paint(state, render_header, hint=hint, prompt=prompt)
        result = state.press(read_key())
        if result is not None:
            click.echo()
            return result
