"""Parse PowerShell with PowerShell's own parser, and never run it.

A quoting test that compares strings only proves what WE believe the quote
rules are. This asks PowerShell. The text goes through
``[System.Management.Automation.Language.Parser]::ParseInput``, which builds
the syntax tree and executes nothing, and every command in the tree comes back
with its elements: parameters by name, string constants with their parsed
VALUE and quote kind. Windows only (``powershell.exe``); the output is ASCII
(base64 for every value), so no console code page can bend it.
"""

from __future__ import annotations

import base64
import os
import subprocess
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path

# Only .NET calls and language keywords: no cmdlet, so nothing autoloads a
# module (which is what writes a ModuleAnalysisCache under the redirected home).
_PARSER = r"""
$text = [IO.File]::ReadAllText($env:PS_PARSE_INPUT, [Text.Encoding]::UTF8)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
function b64([string]$s) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($s)) }
foreach ($e in $errors) { [Console]::Out.WriteLine("error`t" + (b64 $e.Message)) }
$isCommand = { param($n) $n -is [System.Management.Automation.Language.CommandAst] }
foreach ($c in $ast.FindAll($isCommand, $true)) {
  [Console]::Out.WriteLine("command")
  foreach ($el in $c.CommandElements) {
    if ($el -is [System.Management.Automation.Language.CommandParameterAst]) {
      [Console]::Out.WriteLine("param`t" + (b64 $el.ParameterName))
    } elseif ($el -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
      [Console]::Out.WriteLine("const`t" + (b64 $el.StringConstantType) + "`t" + (b64 $el.Value))
    } else {
      [Console]::Out.WriteLine("other`t" + (b64 $el.GetType().Name) + "`t" + (b64 $el.Extent.Text))
    }
  }
}
"""


class Parsed(NamedTuple):
    errors: list[str]
    # one list per command, in tree order; each element is
    # ("param", name) | ("const", quote kind, value) | ("other", type, text)
    commands: list[list[tuple[str, ...]]]

    def named(self, name: str) -> list[list[tuple[str, ...]]]:
        """The commands whose first element is the bare word ``name``."""
        return [c for c in self.commands if c and c[0][-1].lower() == name.lower()]


def parse(text: str, tmp_path: Path) -> Parsed:
    """PowerShell's own parse of ``text``. Nothing in ``text`` is executed."""
    src = tmp_path / "parse-input.ps1"
    src.write_text(text, encoding="utf-8")
    encoded = base64.b64encode(_PARSER.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        encoding="ascii",
        timeout=60,
        cwd=tmp_path,  # anything PowerShell writes relative to its cwd lands here
        env={**os.environ, "PS_PARSE_INPUT": str(src)},
    )
    assert proc.returncode == 0, proc.stderr
    errors: list[str] = []
    commands: list[list[tuple[str, ...]]] = []
    for line in proc.stdout.splitlines():
        kind, *fields = line.split("\t")
        values = tuple(base64.b64decode(f).decode("utf-8") for f in fields)
        if kind == "error":
            errors.append(values[0])
        elif kind == "command":
            commands.append([])
        elif kind in {"param", "const", "other"}:
            commands[-1].append((kind, *values))
    return Parsed(errors, commands)


def argument_of(command: list[tuple[str, ...]], parameter: str) -> tuple[str, ...]:
    """The element right after ``-parameter`` in one parsed command."""
    for i, element in enumerate(command):
        if element[0] == "param" and element[1].lower() == parameter.lower():
            return command[i + 1]
    raise AssertionError(f"-{parameter} is not in {command!r}")
