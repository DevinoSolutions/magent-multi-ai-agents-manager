"""Unit tests for scripts/lint_rules.py — the MD001-MD005 custom lint layer.

Each rule is proven with a violating snippet (fires) and a conforming snippet
(silent), plus scope checks (which path prefixes each rule applies to). The
module lives under scripts/ (not an importable package), so it is loaded by
file path via importlib rather than a normal import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_LINT_RULES = Path(__file__).resolve().parents[2] / "scripts" / "lint_rules.py"
_spec = importlib.util.spec_from_file_location("lint_rules", _LINT_RULES)
assert _spec is not None and _spec.loader is not None  # a loadable module guard
lint_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_rules)


def _codes(rel, source):
    """The finding codes check_source emits for one (relpath, source) pair."""
    return [f.code for f in lint_rules.check_source(rel, source)]


# ---- MD001: no sys.exit / raise SystemExit outside src/magent/cli/ --------


def test_md001_flags_sys_exit_in_subsystem():
    assert _codes("src/magent/launch.py", "import sys\nsys.exit(1)\n") == ["MD001"]


def test_md001_flags_raise_systemexit_in_subsystem():
    assert _codes("src/magent/launch.py", "raise SystemExit(2)\n") == ["MD001"]


def test_md001_allows_sys_exit_inside_cli():
    assert _codes("src/magent/cli/status.py", "import sys\nsys.exit(1)\n") == []


def test_md001_does_not_apply_outside_src():
    # scripts/ legitimately calls sys.exit (check.py, lint_rules.py both do).
    assert "MD001" not in _codes("scripts/tool.py", "import sys\nsys.exit(1)\n")


# ---- MD002: no sys.platform outside platform/ unless allowlisted -------------

_SYS_PLATFORM = 'import sys\nif sys.platform == "win32":\n    pass\n'


def test_md002_flags_sys_platform_in_unlisted_src():
    assert _codes("src/magent/newthing.py", _SYS_PLATFORM) == ["MD002"]


def test_md002_allows_sys_platform_in_platform_package():
    assert _codes("src/magent/platform/windows.py", _SYS_PLATFORM) == []


def test_md002_allows_an_allowlisted_file():
    assert _codes("src/magent/paths.py", _SYS_PLATFORM) == []


# ---- MD003: no "magent:" literal outside titles.py / cli/attach.py ---------------


def test_md003_flags_md_string_literal():
    assert _codes("src/magent/hotkey.py", 'x = "magent:foo"\n') == ["MD003"]


def test_md003_flags_md_fstring_literal_exactly_once():
    src = 'name = "p"\nx = f"magent:{name}"\n'
    assert _codes("src/magent/hotkey.py", src) == ["MD003"]


def test_md003_allows_the_titles_module():
    assert _codes("src/magent/titles.py", 'PREFIX = "magent:"\n') == []


def test_md003_allows_the_attach_module():
    assert _codes("src/magent/cli/attach.py", 'x = "magent:foo"\n') == []


def test_md003_ignores_md_not_at_string_start():
    assert _codes("src/magent/hotkey.py", 'x = "a magent: b"\n') == []


def test_md003_ignores_prefix_followed_by_space():
    # "magent: " (colon + space) is a brand/message prefix, never a window title
    # (titles are "magent:name" / "magent:[!] name" — no space after the colon).
    assert _codes("src/magent/hotkey.py", 'x = "magent: needs input"\n') == []


# ---- MD004: suppression comments must carry a reason: ------------------------


def test_md004_flags_naked_type_ignore():
    assert _codes("src/magent/x.py", "y = 1  # type: ignore[arg-type]\n") == ["MD004"]


def test_md004_flags_naked_noqa():
    assert _codes("src/magent/x.py", "import os  # noqa: F401\n") == ["MD004"]


def test_md004_flags_naked_ty_ignore():
    assert _codes("src/magent/x.py", "y = 1  # ty: ignore[foo]\n") == ["MD004"]


def test_md004_accepts_type_ignore_with_reason():
    src = "y = 1  # type: ignore[arg-type]  # reason: narrowed above\n"
    assert _codes("src/magent/x.py", src) == []


def test_md004_accepts_noqa_with_reason():
    src = "import os  # noqa: F401  # reason: re-exported on purpose\n"
    assert _codes("src/magent/x.py", src) == []


def test_md004_ignores_prose_that_merely_mentions_noqa():
    assert _codes("src/magent/x.py", "# we cannot use noqa in this spot\n") == []


def test_md004_ignores_marker_inside_a_string_literal():
    # "# type: ignore" here is a STRING token, not a COMMENT — no false positive.
    assert _codes("src/magent/x.py", 's = "# type: ignore"\n') == []


def test_md004_applies_to_tests_and_scripts_too():
    assert _codes("tests/unit/test_x.py", "y = 1  # noqa: E501\n") == ["MD004"]
    assert _codes("scripts/tool.py", "y = 1  # noqa: E501\n") == ["MD004"]


# ---- scoping: MD001/002/003 are src-only; MD004 spans src+scripts+tests ------


def test_ast_rules_do_not_run_outside_src_magent():
    # sys.exit + sys.platform + an "magent:" literal in a test file → none of MD001-3.
    src = 'import sys\nsys.exit(1)\nif sys.platform:\n    x = "magent:z"\n'
    assert _codes("tests/unit/test_x.py", src) == []


def test_syntax_error_surfaces_as_md000():
    assert _codes("src/magent/broken.py", "def (:\n") == ["MD000"]


# ---- MD005: no module-level heavy-subsystem import inside src/magent/cli/ --

_CLI = "src/magent/cli/foo.py"


def test_md005_flags_toplevel_from_heavy_subsystem():
    assert _codes(_CLI, "from magent.launch import run_magent\n") == ["MD005"]


def test_md005_flags_toplevel_import_heavy_module():
    assert _codes(_CLI, "import magent.upload_server\n") == ["MD005"]


def test_md005_flags_from_package_import_of_heavy_submodule():
    # `from magent import attention` binds the heavy attention module.
    assert _codes(_CLI, "from magent import attention\n") == ["MD005"]


def test_md005_flags_platform_backend_import():
    assert _codes(_CLI, "from magent.platform import windows\n") == ["MD005"]


def test_md005_flags_relative_heavy_import():
    assert _codes(_CLI, "from ..launch import run_magent\n") == ["MD005"]


def test_md005_allows_get_platform_at_toplevel():
    # The platform package __init__ is light; only the OS backends are heavy.
    assert _codes(_CLI, "from magent.platform import get_platform\n") == []


def test_md005_allows_heavy_import_in_body():
    src = (
        "def cmd():\n    from magent.launch import run_magent\n    return run_magent\n"
    )
    assert _codes(_CLI, src) == []


def test_md005_allows_heavy_import_under_type_checking():
    src = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from magent import attention\n"
    assert _codes(_CLI, src) == []


def test_md005_allows_leaf_and_sibling_cli_imports_at_toplevel():
    src = "from magent.config import load_config\nfrom magent.cli.app import main\n"
    assert _codes(_CLI, src) == []


def test_md005_does_not_apply_outside_cli():
    # A subsystem importing another subsystem at top level is allowed (upload_server does).
    assert _codes("src/magent/upload_server.py", "from magent.launch import x\n") == []
