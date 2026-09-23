"""Drift pin for `magent docs`: the "## CLI commands" table vs. the real
click registry.

`_SETTINGS_FIELD_DOCS` and `_PROJECT_FIELD_DOCS` are hand-written against
dataclasses a reader can diff by eye. `_CLI_COMMAND_DOCS` is hand-written
against *nothing* -- and it rotted: `send`, `model`, `peek`, `doctor`, `watch`,
`attention`, `mobile`, `termius` and `hooks` all shipped with no row at all, and
`sessions --json` went undocumented while plain `sessions` stayed. That matters
more than a missing paragraph here, because `magent docs` advertises itself as
the thing you "pipe to file for AI context": an agent handed that file is told a
smaller CLI exists than the one installed.

So walk the registry and require a row per command. The table stays
hand-written: its descriptions are curated prose (`magent down` explains the
last-attached-host fallback), which a generator would replace with click's terse
one-liners. This test is what keeps hand-written honest.

It asserts on the GENERATED output rather than on `_CLI_COMMAND_DOCS` directly,
so it keeps holding if the table ever does become generated.

Both directions are pinned. A command with no row is the rot that happened; a
row naming a command that no longer exists is the worse half, because an agent
reading the file will actually *run* it.
"""

from __future__ import annotations

import re

import click
import pytest

from magent import cli
from magent.cli.docs import _SETTINGS_FIELD_DOCS, _generate_docs
from magent.config import Settings, settings_to_dict

# A token that could be a command name: lowercase word, possibly hyphenated.
# Everything else in a row -- `--json`, `<host>`, `[host]`, `9090` -- is an
# option, a placeholder or an argument, and names no command.
_COMMAND_TOKEN = re.compile(r"^[a-z][a-z0-9-]*$")


def _cli_command_cells() -> list[str]:
    """The first cell of every row in the generated "## CLI commands" table."""
    doc = _generate_docs()
    lines = doc.splitlines()
    start = lines.index("## CLI commands")
    cells: list[str] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip()
        if cell in {"Command", "---"}:
            continue
        cells.append(cell.strip("`"))
    return cells


def _registry_leaves() -> list[tuple[str, ...]]:
    """Every runnable command path under `main`, as tuples.

    A group contributes its subcommands, not itself: the table documents
    `magent config show`, never a bare `magent config`. Importing `magent.cli`
    is what registers all of them -- the hub runs each module's
    `@main.command` decorator.
    """
    leaves: list[tuple[str, ...]] = []
    for name, command in sorted(cli.main.commands.items()):
        if isinstance(command, click.Group):
            leaves.extend((name, sub) for sub in sorted(command.commands))
        else:
            leaves.append((name,))
    return leaves


def _documents(cell: str, path: tuple[str, ...]) -> bool:
    """Does this row's command cell document `path`?

    Prefix match on whole tokens, so `magent up --json` documents `up` and
    `magent account pin <project> <account>` documents `account pin`, without
    this test having to parse anyone's argument grammar.
    """
    prefix = " ".join(("magent", *path))
    return cell == prefix or cell.startswith(prefix + " ")


def _path_in_cell(cell: str) -> tuple[str, ...]:
    """The command path a row *claims*, read conservatively: the leading
    command-shaped tokens after `magent`, and nothing else. A row about a
    root-group option (`magent --go`) claims no path at all."""
    tokens = cell.split()[1:]  # drop "magent"
    path: list[str] = []
    for token in tokens[:2]:  # no group is nested deeper than one level
        if not _COMMAND_TOKEN.match(token):
            break
        path.append(token)
    return tuple(path)


class TestEveryCommandHasARow:
    def test_every_registered_command_is_documented(self):
        cells = _cli_command_cells()
        missing = [
            path
            for path in _registry_leaves()
            if not any(_documents(cell, path) for cell in cells)
        ]
        assert not missing, (
            "`magent docs` does not document: "
            + ", ".join("magent " + " ".join(p) for p in missing)
            + " -- add a row to _CLI_COMMAND_DOCS in src/magent/cli/docs.py, "
            "describing what the command is FOR and what is surprising about "
            "it rather than restating its --help."
        )

    def test_the_table_was_actually_found(self):
        # Guards the pin itself: a renamed heading, or a switch away from a
        # Markdown table, would empty `cells` and make the assertion above
        # pass vacuously.
        cells = _cli_command_cells()
        assert cells, "no '## CLI commands' table in the generated reference"
        assert all(cell.startswith("magent") for cell in cells)
        assert len(cells) >= len(_registry_leaves())


class TestEveryRowNamesARealCommand:
    def test_no_row_documents_a_command_that_no_longer_exists(self):
        stale: list[str] = []
        for cell in _cli_command_cells():
            path = _path_in_cell(cell)
            if not path:
                continue  # a root-group option row, e.g. `magent --go`
            command = cli.main.commands.get(path[0])
            if command is None:
                stale.append(cell)
                continue
            if (
                len(path) == 2
                and isinstance(command, click.Group)
                and path[1] not in command.commands
            ):
                stale.append(cell)
        assert not stale, (
            "`magent docs` documents commands that are not registered: "
            + ", ".join(f"`{c}`" for c in stale)
            + " -- a reader (or an agent) will try to run these."
        )


# Settings keys that are user-keyed MAPS: their members are named by the user
# (a tool name), so each is documented as one row whose prose carries the inner
# shape, and recursing would demand a row per member. A new open map belongs
# here deliberately -- until it is added the pin fails naming its members,
# which is the loud version of the same decision.
_OPEN_MAPS = frozenset({"tools"})


def _emitted_settings_keys() -> list[str]:
    """Every settings field the factory emits, as dotted names.

    `settings_to_dict` is the one serializer every config generator delegates
    to, so it is the honest answer to "what settings exist" -- flatter than
    walking the dataclasses and already in the config file's own spelling
    (`stalenessWorkingS`, not `staleness_working_s`).
    """
    names: list[str] = []

    def walk(prefix: str, block: dict[str, object]) -> None:
        for key, value in block.items():
            dotted = f"{prefix}{key}"
            # An EMPTY dict counts as a leaf too, so a block that happens to be
            # empty under the defaults can never silently vanish from the
            # expected set -- it must be documented or fail.
            if dotted in _OPEN_MAPS or not isinstance(value, dict) or not value:
                names.append(dotted)
                continue
            walk(f"{dotted}.", value)

    walk("", settings_to_dict(Settings()))
    return names


class TestEverySettingsFieldHasARow:
    """Same rot, the other table. `_SETTINGS_FIELD_DOCS` was hand-written
    against the dataclasses and five real `attention.*` timing fields never got
    a row -- `magent attention --help` even advertised "default:
    attention.pollIntervalS from config" for a field the reference did not
    name."""

    def test_every_emitted_settings_field_is_documented(self):
        documented = {name for name, *_ in _SETTINGS_FIELD_DOCS}
        missing = [k for k in _emitted_settings_keys() if k not in documented]
        assert not missing, (
            "`magent docs` does not document these settings fields: "
            + ", ".join(missing)
            + " -- add a row to _SETTINGS_FIELD_DOCS in src/magent/cli/docs.py, "
            "saying what changing the field COSTS rather than restating its "
            "default."
        )

    def test_no_row_documents_a_field_the_factory_does_not_emit(self):
        emitted = set(_emitted_settings_keys())
        stale = [name for name, *_ in _SETTINGS_FIELD_DOCS if name not in emitted]
        assert not stale, (
            "`magent docs` documents settings fields the factory does not emit: "
            + ", ".join(stale)
            + " -- a reader will set these and nothing will happen."
        )

    def test_the_settings_table_reaches_the_output(self):
        # Ties the two assertions above to what a user actually reads: a row in
        # the list that never rendered would satisfy them both.
        doc = _generate_docs()
        for name, *_ in _SETTINGS_FIELD_DOCS:
            assert f"| `{name}` |" in doc


class TestTheMachineReadableFlagsKeepTheirRows:
    """The registry pin above is structurally blind to a FLAG: a bare `magent
    sessions` row satisfies it while `--json` stays invisible. These two are
    not cosmetic -- they are the read surfaces something else polls
    (`up --json` is what `magent attach` reads over SSH; `sessions --json` is
    what scripts poll), and `sessions --json` lives in a different module
    from the command it hangs off (`session_picker._emit_sessions_json`),
    which is exactly how it went undocumented in the first place."""

    @pytest.mark.parametrize("row", ["magent sessions --json", "magent up --json"])
    def test_the_json_read_surfaces_are_documented(self, row):
        assert any(cell.startswith(row) for cell in _cli_command_cells())
