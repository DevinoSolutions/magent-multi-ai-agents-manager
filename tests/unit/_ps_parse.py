"""Parse PowerShell with PowerShell's own parser, and never run it.

A quoting test that compares strings only proves what WE believe the quote
rules are. This asks PowerShell. ``parse`` hands text to
``[System.Management.Automation.Language.Parser]::ParseInput``; ``parse_file``
hands a FILE to ``::ParseFile``, which decodes it exactly as
``powershell.exe -File`` does (a BOM picks the encoding; without one, Windows
PowerShell 5.1 reads the ANSI code page). Either way the parser builds the
syntax tree and executes nothing, and every command in the tree comes back
with its elements: parameters by name, string constants with their parsed
VALUE and quote kind. Windows only; the output is ASCII (base64 for every
value), so no console code page can bend it.

Windows PowerShell 5.1 and nothing else, because it is the production host
(the launcher is ``powershell.exe``) and because the host decides the decoding:
PowerShell 7 reads a file with no BOM as UTF-8, so under ``pwsh`` an encoding
pin passes against the very writer it exists to catch. The executable comes
from the system directory, never PATH, and the script refuses to parse on any
other major version.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

# Only .NET calls and language keywords: no cmdlet, so nothing autoloads a
# module (which is what writes a ModuleAnalysisCache under the redirected home).
_GUARD = r"""
if ($PSVersionTable.PSVersion.Major -ne 5) {
  [Console]::Error.WriteLine("not Windows PowerShell 5.1: " + $PSVersionTable.PSVersion)
  exit 3
}
"""
# Each reader leaves $ast and $errors behind for _EMIT.
_READ_TEXT = r"""
$text = [IO.File]::ReadAllText($env:PS_PARSE_INPUT, [Text.Encoding]::UTF8)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
"""
_READ_FILE = r"""
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:PS_PARSE_INPUT, [ref]$tokens, [ref]$errors)
"""
_EMIT = r"""
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
    return _run(_READ_TEXT, src, tmp_path)


def parse_file(path: Path, tmp_path: Path) -> Parsed:
    """PowerShell's own parse of the file at ``path``, decoded the way
    ``powershell.exe -File`` decodes it -- so what is tested is the bytes on
    disk, encoding included, not the text we meant to write. Nothing in it is
    executed."""
    return _run(_READ_FILE, path, tmp_path)


def _windows_powershell() -> str:
    """Windows PowerShell's ``powershell.exe`` under the system directory --
    the same place production takes schtasks from, and never a PATH lookup."""
    import ctypes  # win-only: ctypes.windll doesn't exist off Windows

    buffer = ctypes.create_unicode_buffer(260)
    assert ctypes.windll.kernel32.GetSystemDirectoryW(buffer, 260), "no system dir"
    exe = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    assert exe.is_file(), f"Windows PowerShell is not at {exe}"
    return str(exe)


def _run(reader: str, src: Path, tmp_path: Path) -> Parsed:
    program = _GUARD + reader + _EMIT
    encoded = base64.b64encode(program.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [
            _windows_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
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
