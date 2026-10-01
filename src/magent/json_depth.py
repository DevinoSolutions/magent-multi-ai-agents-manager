"""How deeply a JSON text nests, read before json.loads sees it.

json.loads recurses once per level, and what stops it far past any sane depth
depends on the C stack -- RecursionError at 8 MB, JSONDecodeError or a whole
parse at 16 MB and unlimited -- so every reader of JSON magent did not write
(what a node sends, the files it becomes on this PC, the PC files provisioning
ships) asks ``nests_too_deep`` first and refuses in ``TOO_DEEP``'s words, the
same on every interpreter and OS.

A stdlib-only leaf that imports nothing from magent: agent_state, which the
per-turn state hook imports, reads it at no cost, and nodes, node_sync and
remote_mux share it without depending on one another for it.
node_scripts/node_apply.py runs on the node, where nothing from magent is
installed, and carries a copy pinned by tests/unit/test_node_apply.py.
"""

from __future__ import annotations

import re

# Claude Code's own files are a handful of levels; a text nested past this is
# refused whole, before anything parses or walks it.
MAX_JSON_DEPTH = 64
# What each reader says of a text nested past it.
TOO_DEEP = f"nested deeper than {MAX_JSON_DEPTH} levels"

# A JSON string (its escapes read; an unterminated one runs to the end, as
# json reads it) or one bracket: all ``_text_nests_deeper_than`` reads. The
# unrolled loop cannot backtrack: its two parts never start on the same
# character, and the closing quote is optional, so no match ever fails.
_JSON_NESTING_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?|[\[\]{}]', re.DOTALL)


# node_apply.py carries a byte-identical copy of this and of the pattern above
# -- it runs on the node, where nothing from magent is installed -- pinned by
# tests/unit/test_node_apply.py: change them together.
def _text_nests_deeper_than(text: str, limit: int) -> bool:
    """True when the JSON ``text`` opens more than ``limit`` arrays/objects
    inside one another, read before json.loads sees it. json.loads recurses
    once per level, and what stops it far past the bound depends on the C
    stack -- RecursionError on one runner, JSONDecodeError or a whole parse
    on another -- so the refusal is this scan's, the same on every
    interpreter and OS. A bracket inside a string is not nesting."""
    depth = 0
    for match in _JSON_NESTING_TOKEN.finditer(text):
        token = match.group()
        if token in ("[", "{"):
            depth += 1
            if depth > limit:
                return True
        elif token in ("]", "}"):
            depth -= 1
    return False


def nests_too_deep(text: str) -> bool:
    """``_text_nests_deeper_than`` at ``MAX_JSON_DEPTH``: what every reader of
    node JSON -- the pull, the sample, the mirror's records, the node map,
    the marks, the sessions and load files -- and of a PC file provisioning
    ships asks before json.loads, so each refuses past the bound (saying
    ``TOO_DEEP`` where it says anything) the same on every stack."""
    return _text_nests_deeper_than(text, MAX_JSON_DEPTH)
