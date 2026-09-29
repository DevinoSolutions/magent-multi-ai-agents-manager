"""A shipped node script's calls hold under BSD tools (macOS) as under GNU.

GNU getopt permutes, so ``chmod 700 -- dir`` works on Linux; BSD chmod stops
reading options at the mode and takes the ``--`` for a file to change
(bd1b4a1, fixed in f458315). A Linux-only run cannot see that, so this is a
static pin, run on every OS: in every script under ``magent/node_scripts``,
no chmod, chown, mkdir, rm, ln, mv or cp call puts the option terminator
``--`` after a word that is not an option.

The rule, simple on purpose:

- A call is one of those commands at a command position (a line's start, or
  after a blank, separator, paren or quote), then words up to a ``--`` word.
  It is one logical line (backslash-newline continues it), and never crosses
  ``;``, ``&``, ``|`` or ``)``.
- Every word before the ``--`` must be an option (it starts with ``-``), or
  the value of an option that takes its value as its own word
  (``_VALUE_OPTIONS``: ``mkdir -m 700 -- dir``).
- Comments are not code. A line whose first non-blank is ``#``, and a
  trailing `` #`` comment, are dropped first.

So ``chmod 700 -- x`` fails and ``chmod -- 700 x`` passes."""

from __future__ import annotations

import re
from importlib import resources

import pytest

_COMMANDS = ("chmod", "chown", "mkdir", "rm", "ln", "mv", "cp")
# An option whose value is its own word, by command: that word is not an
# operand. A new one must join this, or its value fails the pin -- loudly.
_VALUE_OPTIONS = {"mkdir": frozenset({"-m"})}
_CALL = re.compile(
    r"(?:^|(?<=[\s;&|(!{'\"]))"
    rf"({'|'.join(_COMMANDS)})"
    r"((?:[ \t]+[^\s;&|)]+)*?)[ \t]+--(?=\s|$)"
)
_COMMENT = re.compile(r"(?:^|[ \t])#.*$")


def _calls(text: str) -> list[tuple[str, list[str]]]:
    """Each call of a listed command that reaches a ``--``: the command, and
    the words between it and that ``--``."""
    calls: list[tuple[str, list[str]]] = []
    for line in text.replace("\\\n", " ").splitlines():
        for m in _CALL.finditer(_COMMENT.sub("", line)):
            calls.append((m.group(1), m.group(2).split()))
    return calls


def _misplaced(text: str) -> list[str]:
    """The calls whose ``--`` comes after an operand."""
    bad: list[str] = []
    for command, words in _calls(text):
        takes_a_value = _VALUE_OPTIONS.get(command, frozenset())
        value_next = False
        for word in words:
            if value_next:
                value_next = False
            elif word.startswith("-"):
                value_next = word in takes_a_value
            else:
                bad.append(" ".join([command, *words, "--"]))
                break
    return bad


def _shipped() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in resources.files("magent.node_scripts").iterdir()
        if p.name.endswith(".sh")
    }


class TestNoTerminatorFollowsAnOperand:
    @pytest.mark.parametrize(
        "line",
        [
            'chmod 700 -- "$1"',
            "chown demo -- x",
            "mkdir -p x -- y",
            'mkdir -m 700 "$d" -- y',
            "rm -f x -- y",
            "ln -s a -- b",
            "mv a -- b",
            "cp -p a -- b",
            'x && chmod 700 -- "$1"',
            "trap 'rm -rf a -- b' EXIT",
            "d=$(mkdir x -- y)",
            "chmod 700 \\\n    -- x",
        ],
    )
    def test_the_rule_refuses_a_terminator_after_an_operand(self, line):
        assert _misplaced(line) != []

    @pytest.mark.parametrize(
        "line",
        [
            'chmod -- 700 "$1"',
            'mkdir -m 700 -- "$1"',
            'mkdir -p -- "$(dirname -- "$d")"',
            'rm -rf -- "$work"',
            'mv -f -- "$tmp" "$target"',
            'cp -- "$a" "$b"',
            "find . -type d -exec chmod 700 {} +",
            "# chmod 700 -- x is the form this refuses",
            'chmod 700 "$1"  # a -- in the comment only',
            'chmod 700 "$1" || die -- x',
            "rm x; ls -- y",
            "charm 700 -- x",
        ],
    )
    def test_the_rule_passes_every_other_form(self, line):
        assert _misplaced(line) == []

    def test_no_shipped_script_puts_a_terminator_after_an_operand(self):
        # D's bring_up.sh included: every file under node_scripts is read.
        scripts = _shipped()
        assert {"install_transcripts.sh", "bring_up.sh"} <= set(scripts)
        misplaced = {name: _misplaced(text) for name, text in scripts.items()}
        assert {name: bad for name, bad in misplaced.items() if bad} == {}

    def test_the_scan_reads_the_calls_it_guards(self):
        # Not vacuous: private_dir's chmod and mkdir, as shipped, are seen.
        calls = _calls(_shipped()["install_transcripts.sh"])
        assert ("chmod", []) in calls
        assert ("mkdir", ["-m", "700"]) in calls
