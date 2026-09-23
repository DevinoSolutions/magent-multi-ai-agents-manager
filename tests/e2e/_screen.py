"""A terminal, modelled just far enough to say what is ON SCREEN.

Not a test module (no ``test_`` prefix, so pytest never collects it), and not a
fake of anything the product does. The bytes fed in here are produced by REAL
product code writing to a REAL pseudo-terminal (see ``_pty.Pty``); this is the
oracle that turns that byte stream into the thing the assertions are actually
about -- the grid of characters a human would be looking at.

Why it has to exist: a pty hands back what the child WROTE, not what the
terminal DREW. "The user's half-typed prompt is still on screen" is a statement
about the drawn grid, and no amount of substring matching on the raw stream can
make it, because the whole question is whether an erase landed on that row. A
real terminal emulator (pyte, xterm) would answer it; neither is a dependency of
this project, and the subset needed is small, so it lives here.

MODELLED, because the streams under test use exactly these: printable text with
wrap, CR, LF (scrolling at the bottom margin), CUP (``ESC[r;cH``, clamped the
way real terminals clamp it), ED (``ESC[2J``), EL (``ESC[K`` / ``ESC[1K`` /
``ESC[2K``), the alternate screen (``ESC[?1049h`` / ``l``, a SECOND grid, and
the "cleared on the way out" behaviour that makes leaving it destructive), and
DECSC/DECRC (``ESC7`` / ``ESC8``) cursor save/restore. Everything else -- SGR
colours, mouse-mode toggles, device queries, ConPTY's own chatter -- is parsed
and discarded, which is the correct rendering of a sequence that moves nothing
and prints nothing.

NOT modelled, deliberately: scroll regions (DECSTBM), origin mode, tabs,
character sets, insert/delete line. Nothing under test emits them, and a
half-implemented version would be worse than an absent one -- so an unknown
sequence is dropped rather than guessed at.
"""

from __future__ import annotations

import re

# One escape sequence, of any of the shapes that reach a real pane. Ordered so
# the two-character forms cannot swallow the start of a CSI.
_SEQ = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[78]"  # DECSC / DECRC
    r"|\x1b[@-Z\\-_]"  # lone two-character escapes
)
_CSI = re.compile(r"\x1b\[(\??)([0-9;]*)[ -/]*([@-~])")


class Screen:
    """A ``rows`` x ``cols`` grid of characters, plus an alternate one."""

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols
        self._grid = self._blank()
        self._alt: list[list[str]] | None = None
        self.row = 0
        self.col = 0
        self._saved = (0, 0)
        # Every LF that actually scrolled the screen. The status line's whole
        # promise is that it never causes one, so the count is an assertion
        # target rather than bookkeeping.
        self.scrolls = 0

    # -- state ---------------------------------------------------------------

    def _blank(self) -> list[list[str]]:
        return [[" "] * self.cols for _ in range(self.rows)]

    @property
    def in_alt_screen(self) -> bool:
        return self._alt is not None

    def line(self, row: int) -> str:
        """Row ``row`` (0-based) as it renders, trailing blanks trimmed."""
        return "".join(self._grid[row]).rstrip()

    @property
    def lines(self) -> list[str]:
        return [self.line(r) for r in range(self.rows)]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def row_of(self, needle: str) -> int:
        """The 0-based row currently showing ``needle``, or -1."""
        for index, line in enumerate(self.lines):
            if needle in line:
                return index
        return -1

    # -- feeding -------------------------------------------------------------

    def feed(self, data: str) -> Screen:
        pos = 0
        for match in _SEQ.finditer(data):
            self._text(data[pos : match.start()])
            self._escape(match.group(0))
            pos = match.end()
        self._text(data[pos:])
        return self

    def _text(self, chunk: str) -> None:
        for ch in chunk:
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self._linefeed()
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch in ("\x07", "\x00"):
                continue
            elif ch == "\t":
                self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
            else:
                if self.col >= self.cols:  # wrap, exactly as a terminal does
                    self.col = 0
                    self._linefeed()
                self._grid[self.row][self.col] = ch
                self.col += 1

    def _linefeed(self) -> None:
        if self.row < self.rows - 1:
            self.row += 1
            return
        # At the bottom margin an LF scrolls. In the NORMAL screen the top row
        # goes to scrollback; in the ALTERNATE screen there is no scrollback,
        # so it is simply gone -- which is why the status line never emits one.
        self._grid.pop(0)
        self._grid.append([" "] * self.cols)
        self.scrolls += 1

    def _escape(self, seq: str) -> None:
        if seq == "\x1b7":
            self._saved = (self.row, self.col)
            return
        if seq == "\x1b8":
            self.row, self.col = self._saved
            return
        csi = _CSI.fullmatch(seq)
        if csi is None:
            return
        private, params, final = csi.groups()
        values = [int(p) for p in params.split(";") if p.isdigit()]
        first = values[0] if values else 0
        if private == "?":
            self._private(first, final)
        elif final == "H" or final == "f":
            # CUP is 1-based. Clamped here the way a VT terminal clamps it --
            # but note that the product does NOT rely on that clamping: on
            # Windows the sequence is interpreted by colorama, which silently
            # drops an out-of-range row instead. See attach_client's
            # `SAVE_CURSOR` block.
            wanted_row = (values[0] if values else 1) or 1
            wanted_col = (values[1] if len(values) > 1 else 1) or 1
            self.row = min(self.rows, wanted_row) - 1
            self.col = min(self.cols, wanted_col) - 1
        elif final == "A":
            self.row = max(0, self.row - max(first, 1))
        elif final == "B":
            self.row = min(self.rows - 1, self.row + max(first, 1))
        elif final == "C":
            self.col = min(self.cols - 1, self.col + max(first, 1))
        elif final == "D":
            self.col = max(0, self.col - max(first, 1))
        elif final == "G":
            self.col = min(self.cols, max(first, 1)) - 1
        elif final == "K":
            self._erase_line(first)
        elif final == "J":
            self._erase_display(first)

    def _erase_line(self, mode: int) -> None:
        row = self._grid[self.row]
        if mode == 1:
            span = range(min(self.col + 1, self.cols))
        elif mode == 2:
            span = range(self.cols)
        else:
            span = range(self.col, self.cols)
        for col in span:
            row[col] = " "

    def _erase_display(self, mode: int) -> None:
        if mode == 2:
            self._grid = self._blank()
            return
        rows = range(self.row) if mode == 1 else range(self.row + 1, self.rows)
        for row in rows:
            self._grid[row] = [" "] * self.cols
        self._erase_line(0 if mode != 1 else 1)

    def _private(self, mode: int, final: str) -> None:
        if mode != 1049:
            return  # mouse reporting, bracketed paste, cursor visibility, ...
        if final == "h" and self._alt is None:
            self._alt = self._grid
            self._saved = (self.row, self.col)
            self._grid = self._blank()
        elif final == "l" and self._alt is not None:
            # xterm's 1049 discards the alternate buffer on the way out -- the
            # reason this module never sends it during an outage.
            self._grid = self._alt
            self._alt = None
            self.row, self.col = self._saved
