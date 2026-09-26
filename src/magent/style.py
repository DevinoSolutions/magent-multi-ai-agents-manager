"""Shared click.style shortcut, hoisted out of cli.py and launch.py where it
was independently defined twice (LS-A-003, duplication). Call sites use
`style` directly; the repo-wide S -> style rename (E10) retired the earlier
transitional `S` alias.

`stdout_safe` lives beside it: every command that echoes text magent did not
write (a pane, a node's report) routes that text through it.
"""

from __future__ import annotations

import sys

import click

style = click.style


def stdout_safe(text: str) -> str:
    """``text`` reduced to what THIS process's stdout can actually encode.

    Text magent did not write carries whatever glyphs its author chose. A pane
    is the AGENT's screen: Claude Code's input caret (U+276F), the footer's
    middle dot (U+00B7), box-drawing rules. A node's doctor report is decoded
    with ``errors="replace"``, so one bad byte is U+FFFD. magent's own output
    obeys an ASCII-only rule (see ``psmux``'s status-bar comments); text
    captured from someone else cannot.

    On Windows a REDIRECTED stdout is the legacy code page -- measured cp1252 on
    a stock box -- and echoing a real Claude Code pane through it died with
    ``UnicodeEncodeError`` and exit 1. So ``magent peek proj`` worked in a
    console and CRASHED as ``magent peek proj > tail.txt`` or ``| findstr``.
    Unencodable characters become ``?``: losing a glyph is strictly better than
    losing the command. The symmetric move to the ``errors="replace"`` decodes
    of ``psmux.capture_pane`` and ``remote_mux``.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
    except LookupError:
        # An stdout naming a codec this interpreter does not have. Nothing can
        # be transcoded, and refusing to print would be the worse answer.
        return text
