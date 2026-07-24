#!/usr/bin/env python
"""Custom repo-rule lint layer — the rules ruff has no plugin API for.

Stdlib-only (`ast`, `tokenize`, `io`, `pathlib`). Emits one
``path:line:col: MDxxx message`` per finding and exits 1 if any fire, 0 if
clean. Wired into ``scripts/check.py`` (both modes) and covered snippet-by-
snippet in ``tests/unit/test_lint_rules.py``.

Rules
-----
MD001  No ``sys.exit(...)`` / ``raise SystemExit`` outside ``src/magent/cli/``.
       Subsystems return ints or raise; the exit decision lives in the shells.
MD002  No ``sys.platform`` outside ``src/magent/platform/`` unless the file is
       on the reasoned MD002_ALLOW list (genuinely OS-behavioral dispatch, not a
       capability gate — capability questions use ``Platform.supports_*()``).
MD003  No ``"magent:"`` string / f-string literal outside ``titles.py`` and
       ``cli/attach.py``. The window-title prefix is built only from
       ``titles.MAGENT_TITLE_PREFIX``.
MD004  Every suppression comment (``# noqa`` / ``# type: ignore`` / ``# ty: ignore``)
       must carry a ``reason:`` text — the no-naked-suppressions policy, mechanized.
MD005  No *module-level* import of a heavy subsystem (``launch``, ``upload_server``,
       ``discover``, ``agent_state``, ``attention``, ``hotkey``, and the platform
       backends ``platform.windows`` / ``macos`` / ``linux``) inside
       ``src/magent/cli/``. The cli registration hub imports every command module
       eagerly, so a top-level heavy import makes ``magent --help`` pay that
       subsystem's startup cost — they are imported in-body instead. Exempt:
       ``from magent.platform import get_platform`` (the package ``__init__`` is
       light; only the OS backend modules are heavy) and ``if TYPE_CHECKING:`` imports
       (never executed at runtime).

Scopes: MD001/002/003 apply to ``src/magent/`` only; MD005 to
``src/magent/cli/`` only; MD004 applies to ``src`` + ``scripts`` + ``tests``.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

SRC_PREFIX = "src/magent/"
CLI_PREFIX = "src/magent/cli/"
PLATFORM_PREFIX = "src/magent/platform/"

# MD002: files outside platform/** that legitimately branch on sys.platform for
# OS-behavioral dispatch — path/env semantics, process primitives, editor /
# terminal / console commands. None are capability-gates-in-disguise (those use
# Platform.supports_*()). Each entry is (relpath -> why it is OS-behavioral).
MD002_ALLOW = {
    "src/magent/paths.py": "config-dir location is per-OS (APPDATA / XDG / Library)",
    "src/magent/discover.py": "session-store paths, path separators, FS case-folding",
    "src/magent/agent_state.py": "state-store path differs per OS",
    "src/magent/launch.py": "Windows job-object breakaway in spawn_detached",
    "src/magent/upload_server.py": "taskkill vs os.kill process termination",
    "src/magent/cli/attention_cmd.py": "taskkill vs os.kill process termination",
    "src/magent/cli/watch.py": "non-blocking keypress polling is per-OS (msvcrt vs select)",
    "src/magent/cli/doctor.py": "terminal-emulator candidates are per-OS (wt vs POSIX list)",
    "src/magent/hotkey.py": "module is Windows-only by construction (raises off-win32)",
    "src/magent/sessions/codex.py": "FS case-insensitivity for session-path matching",
    "src/magent/cli/ui.py": "OS-specific editor command + Windows console UTF-8 fix",
    "src/magent/procs.py": "OpenProcess vs os.kill pid-liveness primitive",
    "src/magent/cli/session_picker.py": "terminal reset (cls vs stty/tput) is per-OS",
    "src/magent/cli/attach.py": "reports the sys.platform value in JSON status (data, not a gate)",
    "src/magent/env.py": "host-env readers (config_base, vscode_storage_base) select per-OS default directories",
    "src/magent/lockfile.py": "msvcrt.locking (Windows) vs fcntl.flock (Unix) — each OS has a different locking API",
    "src/magent/psmux.py": "psmux binary fallback path is per-OS (LOCALAPPDATA on Windows)",
}

# MD003: the only two src files allowed to hold a literal "magent:".
MD003_ALLOW = {"src/magent/titles.py", "src/magent/cli/attach.py"}

# MD004: a comment is a suppression directive when, after its leading '#' and
# spaces, it *begins* with one of these — so prose that merely mentions the word
# (or a marker inside a string literal, which is not a COMMENT token) is exempt.
_SUPPRESSION_STARTS = ("noqa", "type: ignore", "type:ignore", "ty: ignore", "ty:ignore")

# MD005: fully-qualified module paths a src/magent/cli/ module must not import
# at module level (they are imported in-body per the startup-cost policy). Only
# the OS backend modules under platform/ are heavy; the platform package __init__
# (get_platform / find_psmux) is light and stays importable at the top.
HEAVY_CLI_IMPORTS = frozenset(
    {
        "magent.launch",
        "magent.upload_server",
        "magent.discover",
        "magent.agent_state",
        "magent.attention",
        "magent.hotkey",
        "magent.platform.windows",
        "magent.platform.macos",
        "magent.platform.linux",
    }
)


class Finding(NamedTuple):
    path: str  # repo-relative, posix
    line: int
    col: int  # 1-based
    code: str
    message: str


def _is_sys_exit(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sys"
    )


def _is_raise_systemexit(node: ast.AST) -> bool:
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    exc: ast.AST = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "SystemExit"


def _is_sys_platform(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "platform"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _is_title_prefix(text: str) -> bool:
    """True if ``text`` begins a hand-built window title.

    Window titles are ``magent:name`` or ``magent:[!] name`` — the colon is
    always followed immediately by the name or a ``[badge]``, never by a space.
    So ``"magent: foo"`` (colon + space) is a brand/notification/message prefix
    (e.g. ``magent: needs input``), not a title, and is exempt.
    """
    return text.startswith("magent:") and not text.startswith("magent: ")


def _starts_md(node: ast.AST) -> bool:
    """True if node is a str literal, or an f-string, whose text begins the title prefix."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _is_title_prefix(node.value)
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return _is_title_prefix(first.value)
    return False


def _is_type_checking_guard(test: ast.expr) -> bool:
    """True for an ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` test."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_toplevel_imports(tree: ast.Module) -> list[ast.stmt]:
    """Import statements that execute at *module import* time.

    Excludes imports nested in a function/method body (they run only on call —
    the whole point of the in-body policy) and imports guarded by
    ``if TYPE_CHECKING:`` (never executed at runtime, so they add no startup cost).
    Imports inside module-level ``if`` / ``try`` / ``with`` / class bodies DO run at
    import and are therefore included.
    """
    found: list[ast.stmt] = []

    def visit(node: ast.AST, deferred: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return  # body runs on call, not at import time
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if not deferred:
                found.append(node)
            return
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            for stmt in node.body:
                visit(stmt, True)
            for stmt in node.orelse:  # the else-branch DOES run at runtime
                visit(stmt, deferred)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, deferred)

    for stmt in tree.body:
        visit(stmt, False)
    return found


def _imported_module_paths(node: ast.stmt, pkg_parts: list[str]) -> set[str]:
    """Fully-qualified module paths one import statement references.

    Handles ``import a.b``, ``from a.b import c`` (yields both ``a.b`` and
    ``a.b.c`` so a ``from <pkg> import <heavy-submodule>`` is caught), and
    relative imports resolved against ``pkg_parts`` (the module's own package)."""
    paths: set[str] = set()
    if isinstance(node, ast.Import):
        paths.update(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            base = node.module or ""
        else:
            up = pkg_parts[: len(pkg_parts) - (node.level - 1)]
            base = ".".join(up + (node.module.split(".") if node.module else []))
        if base:
            paths.add(base)
            paths.update(f"{base}.{alias.name}" for alias in node.names)
    return paths


def _heavy_import_findings(rel: str, tree: ast.Module) -> list[Finding]:
    """MD005 — module-level heavy-subsystem imports inside src/magent/cli/."""
    pkg_parts = rel.removeprefix("src/").removesuffix(".py").split("/")[:-1]
    out: list[Finding] = []
    for node in _runtime_toplevel_imports(tree):
        hits = _imported_module_paths(node, pkg_parts) & HEAVY_CLI_IMPORTS
        if hits:
            out.append(
                Finding(
                    rel,
                    getattr(node, "lineno", 1),
                    getattr(node, "col_offset", 0) + 1,
                    "MD005",
                    f"module-level import of heavy subsystem '{sorted(hits)[0]}' inside "
                    "cli/ — import it in-body (grep 'heavy subsystem: in-body per policy') "
                    "so `magent --help` does not pay its startup cost",
                )
            )
    return out


def _ast_rules(rel: str, source: str) -> list[Finding]:
    """MD001/002/003 (src/magent/) + MD005 (src/magent/cli/)."""
    out: list[Finding] = []
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as e:
        return [
            Finding(
                rel,
                e.lineno or 1,
                (e.offset or 0) + 1,
                "MD000",
                f"syntax error: {e.msg}",
            )
        ]

    in_cli = rel.startswith(CLI_PREFIX)
    in_platform = rel.startswith(PLATFORM_PREFIX)
    md002_allowed = in_platform or rel in MD002_ALLOW
    md003_allowed = rel in MD003_ALLOW

    # Constants that are pieces of an f-string are counted via their JoinedStr
    # parent (below), never again as standalone literals — so f"magent:{x}" flags once.
    fstring_pieces = {
        id(v)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for v in node.values
        if isinstance(v, ast.Constant)
    }

    for node in ast.walk(tree):
        col = getattr(node, "col_offset", 0) + 1
        line = getattr(node, "lineno", 1)
        if not in_cli and (_is_sys_exit(node) or _is_raise_systemexit(node)):
            out.append(
                Finding(
                    rel,
                    line,
                    col,
                    "MD001",
                    "sys.exit/SystemExit outside cli/ — subsystems return ints or raise; the exit decision lives in the shells",
                )
            )
        if not md002_allowed and _is_sys_platform(node):
            out.append(
                Finding(
                    rel,
                    line,
                    col,
                    "MD002",
                    "sys.platform outside platform/ — gate on a Platform.supports_*() probe, or add a reasoned MD002_ALLOW entry if genuinely OS-behavioral",
                )
            )
        if not md003_allowed and _starts_md(node) and id(node) not in fstring_pieces:
            out.append(
                Finding(
                    rel,
                    line,
                    col,
                    "MD003",
                    'string literal starting "magent:" — the window-title prefix is built only from titles.MAGENT_TITLE_PREFIX',
                )
            )
    if in_cli:
        out += _heavy_import_findings(rel, tree)
    return out


def _suppression_rule(rel: str, source: str) -> list[Finding]:
    """MD004 — every suppression comment must carry a reason: text."""
    out: list[Finding] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            body = tok.string.lstrip("#").strip().lower()
            if (
                body.startswith(_SUPPRESSION_STARTS)
                and "reason:" not in tok.string.lower()
            ):
                out.append(
                    Finding(
                        rel,
                        tok.start[0],
                        tok.start[1] + 1,
                        "MD004",
                        "suppression comment without a `reason:` — every # noqa / # type: ignore / # ty: ignore must state why",
                    )
                )
    except (tokenize.TokenError, IndentationError):
        # A malformed token stream is ruff/compileall's job to report, not ours.
        pass
    return out


def check_source(rel: str, source: str) -> list[Finding]:
    """All findings for one file, keyed by its repo-relative posix path."""
    out: list[Finding] = []
    if rel.startswith(SRC_PREFIX):
        out += _ast_rules(rel, source)
    if rel.startswith(("src/", "scripts/", "tests/")):
        out += _suppression_rule(rel, source)
    return out


def check_tree(root: Path) -> list[Finding]:
    """Scan src/, scripts/, tests/ under root and return sorted findings."""
    findings: list[Finding] = []
    for sub in ("src", "scripts", "tests"):
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            try:
                source = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                findings.append(Finding(rel, 1, 1, "MD000", f"could not read: {e}"))
                continue
            findings += check_source(rel, source)
    return sorted(findings)


def main() -> int:
    findings = check_tree(REPO_ROOT)
    for f in findings:
        print(f"{f.path}:{f.line}:{f.col}: {f.code} {f.message}")
    if findings:
        print(f"\n{len(findings)} custom-rule finding(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
