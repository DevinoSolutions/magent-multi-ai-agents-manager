"""json_depth: the one bound and scan every reader of JSON magent did not
write asks before json.loads, and the leaf shape that lets agent_state (the
per-turn hook's import) share it."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from magent import json_depth


class TestTheBoundEveryReaderShares:
    def test_the_bound_and_its_words(self):
        assert json_depth.MAX_JSON_DEPTH == 64
        assert json_depth.TOO_DEEP == "nested deeper than 64 levels"

    @pytest.mark.parametrize(
        ("text", "deeper"),
        [
            pytest.param("[" * 64 + "]" * 64, False, id="64"),
            pytest.param("[" * 65 + "]" * 65, True, id="65"),
            pytest.param('["' + "[" * 100, False, id="unclosed-string"),
            # Width is not depth: 101 containers, two levels.
            pytest.param("[" + ",".join(["[]"] * 100) + "]", False, id="100-wide"),
        ],
    )
    def test_nests_too_deep_is_the_scan_at_the_bound(self, text, deeper):
        assert json_depth.nests_too_deep(text) is deeper


class TestItIsALeaf:
    def test_it_imports_nothing_but_the_standard_library(self):
        """Walks the whole tree, so an in-body import counts too."""
        tree = ast.parse(Path(json_depth.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        relative = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        ]
        assert imported <= set(sys.stdlib_module_names)
        assert relative == []

    def test_the_hook_loads_nothing_else_from_magent_for_it(self):
        """agent_state is what the per-turn state hook imports: taking the scan
        from this leaf adds it and nothing behind it. A fresh interpreter, so
        no other test's imports can hide one."""
        probe = (
            "import sys; import magent.agent_state; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] == 'magent'))"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        assert done.stdout.strip() == str(
            ["magent", "magent.agent_state", "magent.json_depth"]
        )

    def test_the_scan_is_defined_here_and_in_the_nodes_copy_only(self):
        """nodes, node_sync, remote_mux and agent_state import it; only
        node_apply.py, which runs where nothing from magent is installed,
        carries a (pinned) copy."""
        src = Path(json_depth.__file__).parent
        defined = [
            path.relative_to(src).as_posix()
            for path in sorted(src.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
            and node.name == "_text_nests_deeper_than"
        ]
        assert defined == ["json_depth.py", "node_scripts/node_apply.py"]
