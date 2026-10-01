"""The MAGENT_IDLE_REAP test-isolation law: pinned OFF for every tier, and every
explicit child env a test builds pins it too (a real serve/attention child born
with reaping on would kill this box's live fleet).

Checked per CONSTRUCTION, not per module -- a module that mentions the name in
a comment, or pins it in one builder but not the next, must not pass:

- a dict literal that names a supervisor/boost pin names MAGENT_IDLE_REAP too;
- a function that builds a child env by stripping ``MAGENT_*`` sets
  MAGENT_IDLE_REAP itself (the strip is what makes a builder explicit: the
  child no longer inherits conftest's pin).

test_psmux_boost.py is exempt (it is about the boost itself)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from magent import config, env, reap
from magent.env import get_env
from tests.conftest import FakePlatform

_TESTS = Path(__file__).resolve().parent.parent
_PINS = frozenset(
    {"MAGENT_HOTKEY_SUPERVISOR", "MAGENT_UPLOAD_SUPERVISOR", "MAGENT_PSMUX_BOOST"}
)
_REAP = "MAGENT_IDLE_REAP"
_EXEMPT = frozenset({"test_psmux_boost.py", "test_reap_isolation.py"})


def test_conftest_pins_reaping_off(monkeypatch):
    monkeypatch.setattr("magent.env._cached_env", None)
    assert get_env().idle_reap is False


def test_under_conftest_the_sweep_reads_nothing(monkeypatch):
    # What the pin DOES: a real default config and a platform that would allow
    # it, and the destructive verb returns before looking at a single pane.
    def refuse(*_a: object, **_k: object) -> None:
        pytest.fail("sweep_once read the fleet under conftest's MAGENT_IDLE_REAP=0")

    for seam in (
        "magent.reap.gather",
        "magent.reap._read_one",
        "magent.psmux.eligible_projects",
        "magent.psmux.live_sessions",
        "magent.psmux.pane_trees",
    ):
        monkeypatch.setattr(seam, refuse)
    monkeypatch.setattr("magent.env._cached_env", None)
    cfg = config.MagentConfig(projects=[])
    plat = FakePlatform(supports_psmux=True)
    assert cfg.settings.idle_reap.enabled is True  # the setting alone would allow it
    assert reap.off_reason(cfg, plat) == "off (MAGENT_IDLE_REAP=0)"
    assert reap.sweep_once(cfg, plat=plat) == []


def test_the_env_default_is_on(monkeypatch, tmp_path):
    # The user-approved default, read through the real settings class: no env
    # var, an empty dotenv, a fresh cache.
    monkeypatch.delenv(_REAP, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(env, "ENV_FILE", empty)
    monkeypatch.setattr("magent.env._cached_env", None)
    assert get_env().idle_reap is True


def _str_keys(node: ast.Dict) -> set[str]:
    return {
        k.value
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _names_set(fn: ast.AST) -> set[str]:
    """Every env name a function body sets: ``x["K"] = ...``, a dict literal
    key, ``setenv``/``setdefault("K", ...)``, ``update(K=...)``."""
    names: set[str] = set()
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Store)
            and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)
        ):
            names.add(n.slice.value)
        elif isinstance(n, ast.Dict):
            names |= _str_keys(n)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in {"setenv", "setdefault"} and n.args:
                first = n.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
            elif n.func.attr == "update":
                names |= {kw.arg for kw in n.keywords if kw.arg}
    return names


def _strips_magent(fn: ast.AST) -> bool:
    """True when the body builds a NEW env dict filtered by
    ``.startswith("MAGENT_")`` -- a child env. (An in-process ``delenv`` loop
    over MAGENT_* is not one: the conftest pin is re-read by the next test.)"""
    for comp in ast.walk(fn):
        if not isinstance(comp, ast.DictComp):
            continue
        for n in ast.walk(comp):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "startswith"
                and any(
                    isinstance(a, ast.Constant) and a.value == "MAGENT_" for a in n.args
                )
            ):
                return True
    return False


def _modules() -> list[tuple[str, ast.Module]]:
    out: list[tuple[str, ast.Module]] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path.name in _EXEMPT or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_TESTS).as_posix()
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def test_every_dict_naming_a_supervisor_pin_names_idle_reap():
    missing = [
        f"{rel}:{node.lineno}"
        for rel, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and _str_keys(node) & _PINS
        and _REAP not in _str_keys(node)
    ]
    assert not missing, (
        f"these env dicts pin a supervisor but not {_REAP} -- a child born "
        f"with reaping on can kill the fleet: {missing}"
    )


def test_every_magent_stripping_env_builder_sets_idle_reap():
    missing = [
        f"{rel}:{node.lineno} {node.name}"
        for rel, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _strips_magent(node)
        and _REAP not in _names_set(node)
    ]
    assert not missing, (
        f"these builders strip MAGENT_* (so the child loses conftest's pin) "
        f"but never set {_REAP}: {missing}"
    )


def test_the_scan_sees_a_pin_named_only_in_a_comment_as_missing():
    # The per-construction scan must not be satisfied by a comment -- the
    # substring match it replaces was.
    src = (
        "import os\n"
        "def build():\n"
        "    env = {k: v for k, v in os.environ.items()"
        " if not k.startswith('MAGENT_')}\n"
        "    # MAGENT_IDLE_REAP: not pinned here\n"
        "    return env\n"
    )
    fn = ast.parse(src).body[1]
    assert _strips_magent(fn)
    assert _REAP not in _names_set(fn)
