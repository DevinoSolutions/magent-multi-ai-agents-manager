"""The kill guards tests/conftest.py puts under EVERY test, pinned from a
module that is not reap's own: reap._stop's default snapshot is a refusal, not
the machine's live process table; every in-process kill is refused unless it
names, by identity, a process the test registered; the kernel32 procs hands
out ends a process only inside that guarded kill, and procs' source spells that
kill nowhere else; and reap's source reaches no other way to end one. Nothing
here reaches a real process -- the kills land on recorders, the refused
snapshot is never walked, and the source pins only parse."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from magent import procs, reap
from magent.procs import ProcessIdentity
from tests.conftest import OwnPids, refuse_live_snapshot

_IDENT = ProcessIdentity("node.exe", 600)


class _Kills:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, pid: int, expected: ProcessIdentity) -> int | None:
        self.calls.append(pid)
        return 0


class TestNoLiveProcessTable:
    def test_stops_default_snapshot_here_is_the_refusal(self):
        snapshot = reap._stop.__kwdefaults__["snapshot"]
        assert snapshot is not procs.snapshot_processes
        with pytest.raises(pytest.fail.Exception, match="live process table"):
            snapshot()

    @pytest.mark.live_process_table
    def test_the_marker_hands_back_the_live_default(self):
        # Read only: this test never calls _stop.
        assert reap._stop.__kwdefaults__["snapshot"] is procs.snapshot_processes

    def test_a_renamed_keyword_fails_the_guard_instead_of_passing_it(self, monkeypatch):
        # monkeypatch.setitem ADDS a missing key, so a renamed keyword would
        # leave the guard patching a default nobody reads.
        def renamed(sig, *, table=procs.snapshot_processes):
            return sig, table

        with pytest.raises(AssertionError, match="snapshot"):
            refuse_live_snapshot(renamed, monkeypatch)
        assert renamed.__kwdefaults__ == {"table": procs.snapshot_processes}


class TestOwnProcessesOnly:
    # The wiring is pinned here, and the guard's behaviour below is driven
    # through the object itself: if the wiring ever broke, a call through
    # procs would reach the real primitive at a pid no test owns.
    def test_every_kill_here_goes_through_the_guard(self, own_pids):
        assert procs.terminate_verified == own_pids.terminate_verified

    def test_an_unregistered_pid_is_refused_before_the_kill(self, own_pids):
        kills = _Kills()
        own_pids.kill = kills
        with pytest.raises(pytest.fail.Exception, match="did not spawn"):
            own_pids.terminate_verified(4242, _IDENT)
        assert kills.calls == []

    def test_a_registered_process_reaches_the_kill(self, own_pids):
        kills = _Kills()
        own_pids.kill = kills
        own_pids.add(4242, _IDENT)
        assert own_pids.terminate_verified(4242, _IDENT) == 0
        assert kills.calls == [4242]

    @pytest.mark.parametrize(
        "now_there",
        [
            pytest.param(ProcessIdentity("node.exe", 601), id="later-creation"),
            pytest.param(ProcessIdentity("svc.exe", 600), id="other-image"),
        ],
    )
    def test_a_registered_pid_now_naming_another_process_is_refused(
        self, own_pids, now_there
    ):
        # The process the test spawned died and its pid was reused: the real
        # terminate_verified would verify the reuser as itself, so only the
        # identity key keeps it out.
        kills = _Kills()
        own_pids.kill = kills
        own_pids.add(4242, _IDENT)
        with pytest.raises(pytest.fail.Exception, match="did not spawn"):
            own_pids.terminate_verified(4242, now_there)
        assert kills.calls == []

    def test_the_guard_is_a_plain_object_a_test_can_build(self):
        guard = OwnPids(_Kills())
        with pytest.raises(pytest.fail.Exception, match="pid 7"):
            guard.terminate_verified(7, _IDENT)


class _FakeKernel32:
    """The OS behind the guard's kernel32: a handle per open, and a record of
    every process and job it was told to end."""

    def __init__(self) -> None:
        self.terminated: list[int] = []
        self.jobs: list[int] = []

    def OpenProcess(self, rights: int, inherit: bool, pid: int) -> int:
        return 1000 + pid

    def CloseHandle(self, handle: int) -> int:
        return 1

    def TerminateProcess(self, handle: int, code: int) -> int:
        self.terminated.append(handle)
        return 1

    def TerminateJobObject(self, job: int, code: int) -> int:
        self.jobs.append(job)
        return 1

    def GetExitCodeProcess(self, handle: int, code: object) -> int:
        return 259


def _guard(fake: _FakeKernel32) -> OwnPids:
    guard = OwnPids(_Kills(), kernel32=lambda: fake)
    guard.add(4242, _IDENT)
    return guard


def _ends(guard: OwnPids, target: int, *, close_first: bool = False):
    """A kill primitive that opens ``target`` through the kernel32 the guard
    hands out, and ends it -- or ends the handle after closing it."""

    def kill(pid: int, expected: ProcessIdentity) -> int:
        k = guard.kernel32()
        handle = k.OpenProcess(1, False, target)
        if close_first:
            k.CloseHandle(handle)
            return k.TerminateProcess(handle, 1)
        try:
            return k.TerminateProcess(handle, 1)
        finally:
            k.CloseHandle(handle)

    return kill


class TestTheKernel32ProcsHandsOut:
    def test_procs_hands_out_the_guards_kernel32(self, own_pids):
        assert procs._kernel32 == own_pids.kernel32

    def test_terminate_process_outside_a_guarded_kill_fails_the_test(self):
        fake = _FakeKernel32()
        k = _guard(fake).kernel32()
        handle = k.OpenProcess(1, False, 4242)
        with pytest.raises(pytest.fail.Exception, match="outside the guarded kill"):
            k.TerminateProcess(handle, 1)
        assert fake.terminated == []

    def test_a_guarded_kill_lands_on_the_pid_it_approved(self):
        # The control: without it every refusal below would be vacuous.
        fake = _FakeKernel32()
        guard = _guard(fake)
        guard.kill = _ends(guard, 4242)
        assert guard.terminate_verified(4242, _IDENT) == 1
        assert fake.terminated == [1000 + 4242]

    def test_a_guarded_kill_cannot_land_on_another_pid(self):
        fake = _FakeKernel32()
        guard = _guard(fake)
        guard.kill = _ends(guard, 5151)
        with pytest.raises(pytest.fail.Exception, match="outside the guarded kill"):
            guard.terminate_verified(4242, _IDENT)
        assert fake.terminated == []

    def test_a_closed_handle_ends_nothing(self):
        fake = _FakeKernel32()
        guard = _guard(fake)
        guard.kill = _ends(guard, 4242, close_first=True)
        with pytest.raises(pytest.fail.Exception, match="outside the guarded kill"):
            guard.terminate_verified(4242, _IDENT)
        assert fake.terminated == []

    @pytest.mark.parametrize("raises", [False, True], ids=["returned", "raised"])
    def test_the_approval_ends_with_the_guarded_call(self, raises):
        fake = _FakeKernel32()
        guard = _guard(fake)
        if raises:

            def boom(pid: int, expected: ProcessIdentity) -> int:
                raise RuntimeError("boom")

            guard.kill = boom
            with pytest.raises(RuntimeError):
                guard.terminate_verified(4242, _IDENT)
        else:
            guard.terminate_verified(4242, _IDENT)
        k = guard.kernel32()
        handle = k.OpenProcess(1, False, 4242)
        with pytest.raises(pytest.fail.Exception, match="outside the guarded kill"):
            k.TerminateProcess(handle, 1)
        assert fake.terminated == []

    def test_terminate_job_object_never_lands(self):
        fake = _FakeKernel32()
        guard = _guard(fake)
        guard.kill = lambda pid, expected: guard.kernel32().TerminateJobObject(7, 1)
        with pytest.raises(pytest.fail.Exception, match="TerminateJobObject"):
            guard.terminate_verified(4242, _IDENT)
        assert fake.jobs == []

    def test_every_other_call_passes_through(self):
        assert _guard(_FakeKernel32()).kernel32().GetExitCodeProcess(1, None) == 259


# --- reap's reach: what the runtime guards cannot wrap for every tier ---------
# os.kill, taskkill and psmux kill-server end processes with no guard in
# between, and e2e teardown uses them on its own daemons, so they cannot be
# refused under every test. What reap can reach is pinned from its source.

_REAP = ast.parse(Path(reap.__file__).read_text(encoding="utf-8"))


def _nodes(tree: ast.Module):
    """Every node of ``tree``, with the top-level function it is in."""
    for top in tree.body:
        owner = top.name if isinstance(top, ast.FunctionDef) else None
        for node in ast.walk(top):
            yield owner, node


def _fold(node: ast.AST) -> str | None:
    """A string or bytes constant, or a ``+`` chain of them, ``sep.join`` over a
    literal tuple or list of them, or an f-string of them with no format spec,
    as the one string it builds: "task" + "kill" is the name the scans look
    for, and ctypes takes b"TerminateProcess" as readily as the str. Nothing
    else is folded (a generator join, ``str.join``, ``%``, a format spec); the
    spec lists those as open."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        return node.value.decode("latin-1")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.Tuple, ast.List))
    ):
        sep = _fold(node.func.value)
        parts = [_fold(e) for e in node.args[0].elts]
        if sep is not None and all(p is not None for p in parts):
            return sep.join(p for p in parts if p is not None)
    if isinstance(node, ast.JoinedStr):
        parts = [
            _fold(v.value)
            if isinstance(v, ast.FormattedValue)
            and v.conversion in (-1, ord("s"))
            and v.format_spec is None
            else _fold(v)
            for v in node.values
        ]
        if all(p is not None for p in parts):
            return "".join(p for p in parts if p is not None)
    return None


def _strings(nodes) -> list[tuple[str | None, str]]:
    """``(owner, text)`` for every string constant and folded chain."""
    return [(owner, text) for owner, node in nodes if (text := _fold(node)) is not None]


# What reap reads of each module it imports from magent. One member ends a
# process: procs.terminate_verified, behind the guards above. A new member is
# a review, not a rename: say whether it can end a process, and if it can,
# route it through terminate_verified.
_REACH = {
    "agent_state": {
        "DONE",
        "IDLE",
        "PARKED",
        "norm_cwd",
        "read_record",
        "write_state",
    },
    "config": {"MagentConfig"},
    "env": {"get_env"},
    "log": {"get_logger"},
    "procs": {
        "ProcessIdentity",
        "current_session_id",
        "filetime_to_epoch",
        "precise_filetime",
        "process_identity",
        "process_tree",
        "session_id_of",
        "snapshot_processes",
        "terminate_verified",
    },
    "psmux": {
        "capture_pane",
        "eligible_projects",
        "idle_sessions",
        "image_stem",
        "is_idle_command",
        "live_sessions",
        "pane_trees",
    },
    "fleet": {"classify_state", "input_draft", "paste_and_enter"},
}
# Modules that can end a process with nothing of the guards' in between.
_UNGUARDED = {"os", "signal", "subprocess", "ctypes", "_winapi", "multiprocessing"}
_KILLS = {
    "kill",
    "killpg",
    "terminate",
    "send_signal",
    "pidfd_send_signal",
    "TerminateProcess",
    "TerminateJobObject",
    "close_window",
    "kill_server",
    "kill_servers",
    "stop_sessions",
}
# Every name reap imports from magent, as (module, name). Closed, because a
# module outside the seven above can end a process too (launch.stop_psmux,
# upload_server.stop_server) and the member scan reads only those seven. A new
# import is the same review as a new member.
_MAGENT_IMPORTS = {
    ("magent", "agent_state"),
    ("magent", "config"),
    ("magent", "env"),
    ("magent", "fleet"),
    ("magent", "log"),
    ("magent", "procs"),
    ("magent", "psmux"),
    ("magent.platform", "Platform"),
    ("magent.platform", "get_platform"),
    ("magent.sessions", "AGENT_TOOLS"),
    ("magent.sessions", "AgentTool"),
    ("magent.sessions", "agent_image_names"),
    ("magent.sessions", "agent_image_stem"),
    ("magent.sessions", "build_resume_command"),
    ("magent.sessions", "is_ide_tool"),
    ("magent.sessions.claude", "default_config_dir"),
    ("magent.sessions.live", "LiveSession"),
    ("magent.sessions.live", "SessionScan"),
}
# Every name a `from magent... import` binds in reap.
_BOUND = {name for _module, name in _MAGENT_IMPORTS}
# Everything reap takes from outside magent, as (module, name): a name a
# `from` import binds, or one read off a module reap imports whole. By name,
# not by module: operator.methodcaller, inspect.getmembers and
# pkgutil.resolve_name each reach a member by a string, and so does
# typing.get_type_hints, which evaluates a string annotation; of typing, reap
# takes two names only. A new name is a review, not a convenience.
_REAP_STDLIB = {
    ("__future__", "annotations"),
    ("collections.abc", "Callable"),
    ("collections.abc", "Mapping"),
    ("collections.abc", "Sequence"),
    ("logging", "Logger"),
    ("math", "isfinite"),
    ("pathlib", "Path"),
    ("time", "monotonic"),
    ("time", "sleep"),
    ("time", "time"),
    ("typing", "NamedTuple"),
    ("typing", "TYPE_CHECKING"),
    ("unicodedata", "category"),
}


def _bindings(nodes) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """What the imports among ``nodes`` bind: the module each whole import's
    name stands for (``import a.b`` binds ``a``; ``import a.b as x`` binds
    ``x`` to ``a.b``), and (module, name) for every name a ``from`` binds."""
    whole: dict[str, str] = {}
    taken: set[tuple[str, str]] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    whole[a.asname] = a.name
                else:
                    whole[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            taken |= {(node.module, a.name) for a in node.names}
    return whole, taken


def _members_read(nodes, whole: dict[str, str]):
    """(module, member) for every ``module.member`` read of a name in
    ``whole``, and every other load of one: a module handed on whole is
    read by code no scan here follows."""
    nodes = list(nodes)
    parent = {c: n for n in nodes for c in ast.iter_child_nodes(n)}
    read: set[tuple[str, str]] = set()
    bare: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Name) and node.id in whole:
            up = parent.get(node)
            if isinstance(up, ast.Attribute) and up.value is node:
                read.add((whole[node.id], up.attr))
            else:
                bare.append(f"{node.id} (line {node.lineno})")
    return read, bare


def _platform_attributes() -> set[str]:
    """Every attribute of ``Platform`` and of each of its subclasses in the
    platform package: methods, class attributes and ``self.`` attributes,
    private ones included (WindowsPlatform._schtasks runs a scheduled task)."""
    classes = [
        node
        for path in sorted((Path(reap.__file__).parent / "platform").glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    ]
    ours, grew = {"Platform"}, True
    while grew:
        grew = False
        for cls in classes:
            bases = {ast.unparse(b).rsplit(".", 1)[-1] for b in cls.bases}
            if cls.name not in ours and bases & ours:
                ours.add(cls.name)
                grew = True
    attrs: set[str] = set()
    for cls in (c for c in classes if c.name in ours):
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                attrs.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                attrs |= {t.id for t in targets if isinstance(t, ast.Name)}
        attrs |= {
            node.attr
            for node in ast.walk(cls)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
    return attrs


# A Platform is reached through a value (get_platform(), a parameter), so no
# name scan sees what reap asks of one. These are the only attributes of any
# Platform reap may spell, on anything: WindowsPlatform.launch_psmux_session,
# for one, runs kill-server on a session whose probe fails.
_PLATFORM_ASKS = {
    "logon_session_is_interactive",
    "pane_reset_command",
    "supports_psmux",
}
# Builtins that reach a member or a module by a computed name, which no scan
# of reap's source can read.
_BY_COMPUTED_NAME = {
    "getattr",
    "setattr",
    "delattr",
    "vars",
    "globals",
    "locals",
    "eval",
    "exec",
    "compile",
}
# Modules that hand back any loaded module by a name no import names:
# sys.modules, builtins.__import__, gc.get_referrers.
_EVERY_MODULE = {"sys", "builtins", "gc"}


class TestReapEndsAProcessOnlyThroughTheGuard:
    def test_reap_imports_no_module_that_ends_a_process_unguarded(self):
        imported: set[str] = set()
        for node in ast.walk(_REAP):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "magent" in imported  # the control: the scan sees reap's imports
        assert imported & _UNGUARDED == set()

    def test_reap_reads_the_process_modules_as_modules(self):
        # `from magent.procs import terminate_verified` binds the real kill at
        # import, past the guard's patch of procs; an alias hides the module
        # from the member scan below.
        seams = {f"magent.{name}" for name in _REACH}
        bound = []
        for node in ast.walk(_REAP):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module in seams:
                bound.append(node.module)
            if node.module == "magent":
                bound += [a.name for a in node.names if a.name in _REACH and a.asname]
        assert bound == []

    def test_reap_imports_only_the_known_names_from_magent(self):
        # `import magent.procs [as p]` hides the module from the member scan
        # (`magent.procs.x` and `p.x` are not `procs.x`), a relative import has
        # no module name to check, and a dynamic one has no import statement.
        plain, relative, dynamic, names = [], [], [], set()
        for node in ast.walk(_REAP):
            if isinstance(node, ast.Import):
                roots = [a.name for a in node.names]
                plain += [n for n in roots if n.split(".")[0] == "magent"]
                dynamic += [n for n in roots if n.split(".")[0] == "importlib"]
            elif isinstance(node, ast.ImportFrom) and node.level:
                relative.append(node.module)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root == "importlib":
                    dynamic.append(node.module)
                elif root == "magent":
                    names |= {(node.module, a.name) for a in node.names}
            elif isinstance(node, ast.Name) and node.id == "__import__":
                dynamic.append(node.id)
        assert plain == []
        assert relative == []
        assert dynamic == []
        assert names == _MAGENT_IMPORTS

    def test_reap_calls_only_the_known_members_of_the_process_modules(self):
        used: dict[str, set[str]] = {name: set() for name in _REACH}
        for node in ast.walk(_REAP):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in _REACH
            ):
                used[node.value.id].add(node.attr)
        assert "terminate_verified" in used["procs"]  # the control: it sees the kill
        unknown = {
            name: sorted(members - _REACH[name]) for name, members in used.items()
        }
        assert unknown == {name: [] for name in _REACH}

    def test_reap_loads_a_module_only_to_read_one_member(self):
        # `_pm = psmux`, `(psmux,)[0]`, `f(m=psmux)` and `getmembers(psmux)`
        # each hand the module to code the member scan cannot follow.
        parent = {c: n for n in ast.walk(_REAP) for c in ast.iter_child_nodes(n)}
        loads = [
            node
            for node in ast.walk(_REAP)
            if isinstance(node, ast.Name) and node.id in _REACH
        ]
        assert loads  # the control: it sees the modules reap reads
        bare = [
            f"{node.id} (line {node.lineno})"
            for node in loads
            if not (
                isinstance(parent[node], ast.Attribute) and parent[node].value is node
            )
        ]
        assert bare == []

    def test_every_member_reap_reads_is_its_modules_own(self):
        # agent_state.os is a module in a member's clothing: no member on the
        # list is a name its module imported.
        imported: dict[str, set[str]] = {}
        for name in _REACH:
            tree = ast.parse(
                (Path(reap.__file__).parent / f"{name}.py").read_text(encoding="utf-8")
            )
            imported[name] = {
                alias.asname or alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
        assert "subprocess" in imported["psmux"]  # the control: it reads imports
        assert {name: _REACH[name] & imported[name] for name in _REACH} == {
            name: set() for name in _REACH
        }

    def test_reap_takes_only_the_known_names_from_the_stdlib(self):
        # A module imported whole is read like the magent ones: only as
        # `module.name`, so every name taken from it is in the pairs. Keyed by
        # the name the import binds: `import http.server` binds `http`, and
        # `http.server.os` is a read of it.
        nodes = list(ast.walk(_REAP))
        whole, taken = _bindings(nodes)
        read, bare = _members_read(nodes, whole)
        taken = {(m, n) for m, n in taken if m.split(".")[0] != "magent"}
        assert ("time", "monotonic") in read  # the control: it reads whole modules
        assert bare == []
        assert taken | read == _REAP_STDLIB

    def test_reap_calls_no_kill_by_any_other_name(self):
        called: set[str] = set()
        for node in ast.walk(_REAP):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)
        assert "terminate_verified" in called  # the control
        assert called & _KILLS == set()
        strings = [text for _owner, text in _strings(_nodes(_REAP))]
        spelled = re.compile(r"(?i)taskkill|kill-server|kill-session")
        assert [s for s in strings if spelled.search(s)] == []

    def test_reap_asks_a_platform_for_three_things_only(self):
        # By attribute name, whatever it is spelled on: the variable holding
        # the Platform can be renamed, the attribute cannot. Every subclass
        # counts, since get_platform() hands out one.
        attrs = _platform_attributes()
        # The control: it read the ABC and a subclass-only member.
        assert {"launch_psmux_session", "_schtasks"} <= attrs
        spelled = {n.attr for n in ast.walk(_REAP) if isinstance(n, ast.Attribute)}
        assert spelled & attrs == _PLATFORM_ASKS

    def test_reap_reaches_nothing_by_a_computed_name(self):
        # getattr(psmux, "kill_" + "server"), psmux.__dict__[...] and
        # vars(procs)[...] name a member no scan here can read; so does any
        # dunder, which is how an object hands out its module's namespace. A
        # bare name counts too: __builtins__["__import__"] is __import__.
        computed = [
            n.id
            for n in ast.walk(_REAP)
            if isinstance(n, ast.Name) and n.id in _BY_COMPUTED_NAME
        ]
        assert computed == []
        assert _dunders(_nodes(_REAP)) == []

    def test_reap_reaches_no_module_through_one_it_imports(self):
        # agent_state.os.system(...) and agent_state.sys.modules[...] reach a
        # module reap never imported through one it did: every attribute
        # chain rooted at a name reap bound from magent is one link deep.
        chains = []
        for node in ast.walk(_REAP):
            if isinstance(node, ast.Attribute) and isinstance(
                node.value, ast.Attribute
            ):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in _BOUND:
                    chains.append(ast.unparse(node))
        assert chains == []

    def test_reap_imports_no_door_to_every_module(self):
        imported: set[str] = set()
        for node in ast.walk(_REAP):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "unicodedata" in imported  # the control: it sees stdlib imports
        assert imported & _EVERY_MODULE == set()


# --- procs' own kernel32: the kill is spelled in one function ------------------
# The runtime guard wraps the kernel32 procs._kernel32 hands out. procs builds
# other kernel32 handles of its own (the Toolhelp snapshot, the session and
# priority reads), and a kill spelled over one of those would pass the guard.

_PROCS = ast.parse(Path(procs.__file__).read_text(encoding="utf-8"))
# kernel32's kill and its job-wide twin, ntdll's native kill under both of
# its names, and user32's window-and-process kill.
_ENDERS = re.compile(
    r"NtTerminateProcess|ZwTerminateProcess|TerminateProcess|TerminateJobObject"
    r"|EndTask"
)
# Calls that end a process by a name that is not kernel32's.
_KILL_CALLS = {"kill", "killpg", "terminate", "send_signal", "pidfd_send_signal"}
# The same calls written as text, and os.kill even uncalled (f = os.kill).
_KILL_TEXT = re.compile(r"\bos\.kill|\b(?:" + "|".join(sorted(_KILL_CALLS)) + r")\s*\(")

# Everything procs imports. A new import is a review: whoever adds one extends
# this list, saying why the module cannot end a process past the guard.
_PROCS_IMPORTS = {
    "__future__",
    "collections.abc",
    "ctypes",
    # boot_time's darwin branch: find_library only resolves libc's path, and
    # the one call made through it is sysctlbyname("kern.boottime").
    "ctypes.util",
    "json",
    "os",
    "shutil",
    "subprocess",
    "sys",
    "tempfile",
    "time",
    "typing",
}
# Everything the console helper imports: its own list, for its own process.
_HELPER_IMPORTS = {"ctypes", "json", "sys"}
# Its one `from` import, and every member it reads of each module it binds.
# Closed on purpose, like _PROCS_IMPORTS: a new member (wintypes.HANDLE,
# ctypes.byref) goes red here, and extending the list is a spec change, made
# in review.
_HELPER_FROM = {("ctypes", "wintypes")}
_HELPER_MEMBERS = {
    ("ctypes", "POINTER"),
    ("ctypes", "WinDLL"),
    ("ctypes.wintypes", "BOOL"),
    ("ctypes.wintypes", "DWORD"),
    ("json", "dump"),
    ("sys", "argv"),
}
# Everything the helper reads off its one kernel32. The enders are a deny
# list, and the helper attaches to an agent's console, where
# GenerateConsoleCtrlEvent ends the agent with no TerminateProcess in sight.
# Closed like the list above, and extended the same way.
_HELPER_KERNEL32 = {
    "AttachConsole",
    "FreeConsole",
    "GetConsoleProcessList",
    "GetCurrentProcessId",
    "argtypes",
    "restype",
}


def _procs_nodes():
    return _nodes(_PROCS)


def _helper_nodes():
    """The console helper, parsed: procs runs it as Python of its own, which
    the scans of procs read as one string. A helper that stops being Python
    fails here."""
    texts = [
        node.value.value
        for node in _PROCS.body
        if isinstance(node, ast.Assign)
        and [ast.unparse(t) for t in node.targets] == ["_CONSOLE_HELPER"]
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert len(texts) == 1, texts
    return list(_nodes(ast.parse(texts[0])))


def _named_enders(nodes) -> set[tuple[str | None, str]]:
    """Every ender named by attribute, in a string, or in a folded chain."""
    named = set()
    for owner, node in nodes:
        if isinstance(node, ast.Attribute) and _ENDERS.fullmatch(node.attr):
            named.add((owner, node.attr))
        elif (text := _fold(node)) is not None:
            named |= {(owner, m) for m in _ENDERS.findall(text)}
    return named


def _getattr_reads(nodes) -> list[tuple[str | None, str]]:
    return [
        (owner, ast.unparse(node.args[0]) if node.args else "")
        for owner, node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
    ]


def _dunders(nodes) -> list[str]:
    """``kernel32.__getattr__(...)``, ``__import__(...)``: a dunder hands out
    by name what no scan here can read."""
    names = [
        node.attr if isinstance(node, ast.Attribute) else node.id
        for _owner, node in nodes
        if isinstance(node, (ast.Attribute, ast.Name))
    ]
    return [n for n in names if n.startswith("__") and n.endswith("__")]


def _by_computed_name(nodes) -> list[tuple[str | None, str]]:
    """Every use of a builtin in ``_BY_COMPUTED_NAME``: the whole call where it
    is called, the bare name where it is only loaded (``_g = getattr``)."""
    nodes = list(nodes)
    calls = {id(node.func): node for _o, node in nodes if isinstance(node, ast.Call)}
    return [
        (owner, ast.unparse(calls.get(id(node), node)))
        for owner, node in nodes
        if isinstance(node, ast.Name) and node.id in _BY_COMPUTED_NAME
    ]


def _kill_sites(nodes) -> set[tuple[str | None, str]]:
    """Every place a kill call is named: the whole call where it is called, the
    bare reference where it is only loaded (f = os.kill), and the import where
    it is bound (from os import kill as k)."""
    nodes = list(nodes)
    calls = {id(node.func): node for _o, node in nodes if isinstance(node, ast.Call)}
    sites = set()
    for owner, node in nodes:
        if isinstance(node, ast.alias) and node.name in _KILL_CALLS:
            sites.add((owner, f"import {node.name}"))
        elif (isinstance(node, ast.Attribute) and node.attr in _KILL_CALLS) or (
            isinstance(node, ast.Name) and node.id in _KILL_CALLS
        ):
            sites.add((owner, ast.unparse(calls.get(id(node), node))))
    return sites


def _imports(nodes) -> set[str]:
    modules: set[str] = set()
    for _owner, node in nodes:
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
    return modules


class TestProcsSpellsTheKillOnlyInTerminateVerified:
    def test_the_kernel32_kill_is_called_only_in_terminate_verified(self):
        calls = [
            (owner, node.func.attr)
            for owner, node in _procs_nodes()
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _ENDERS.fullmatch(node.func.attr)
        ]
        assert calls == [("terminate_verified", "TerminateProcess")]

    def test_the_kernel32_kill_is_named_only_where_it_is_declared_and_called(self):
        # _kernel32 declares its argtypes; nothing else may reach it by
        # attribute or spell it in a string (a subscript, a helper script),
        # built with + or not.
        assert _named_enders(_procs_nodes()) == {
            ("_kernel32", "TerminateProcess"),
            ("terminate_verified", "TerminateProcess"),
        }

    def test_a_handle_that_can_end_a_process_is_opened_only_there(self):
        loads = {
            owner
            for owner, node in _procs_nodes()
            if isinstance(node, ast.Name)
            and node.id == "PROCESS_TERMINATE"
            and isinstance(node.ctx, ast.Load)
        }
        assert loads == {"terminate_verified"}

    def test_procs_reads_by_computed_name_only_from_sys(self):
        # getattr(WinDLL("kernel32"), name) spells no name the scans above can
        # read, and neither do kernel32.__getattr__(name), vars(os)["kill"] or
        # a getattr bound under another name. The one computed read is of sys.
        assert _by_computed_name(_procs_nodes()) == [
            ("_helper_python", "getattr(sys, '_base_executable', None)")
        ]
        assert _getattr_reads(_procs_nodes()) == [("_helper_python", "sys")]
        assert _dunders(_procs_nodes()) == []

    def test_every_other_kill_in_procs_is_one_it_makes_today(self):
        # Whole calls, not just names: os.kill(pid, 0) only probes, and a
        # os.kill(pid, 9) beside it must not pass as the same entry. A kill
        # loaded now and called later (f = os.kill) is a site of its own.
        assert _kill_sites(_procs_nodes()) == {
            ("pid_alive", "os.kill(pid, 0)"),  # a liveness probe: signal 0
            ("console_clients", "proc.kill()"),  # the helper procs spawned
        }

    def test_procs_imports_only_the_known_modules(self):
        # operator.methodcaller("kill") ends a process with no kill in sight.
        assert _imports(_procs_nodes()) == _PROCS_IMPORTS

    def test_procs_never_spells_taskkill(self):
        strings = [text for _owner, text in _strings(_procs_nodes())]
        assert any("AttachConsole" in s for s in strings)  # the control: the helper
        assert [s for s in strings if re.search(r"(?i)taskkill", s)] == []

    def test_procs_spells_no_kill_call_in_its_strings(self):
        # The console helper is Python procs runs as a child: a kill written
        # in its text is a string to the call scans above, which read only
        # procs' own code. The helper is parsed below too; this is the cheap
        # half.
        strings = [text for _owner, text in _strings(_procs_nodes())]
        assert any("AttachConsole" in s for s in strings)  # the control: the helper
        assert [m.group() for s in strings for m in _KILL_TEXT.finditer(s)] == []


class TestTheConsoleHelperIsPinnedLikeProcs:
    def test_the_helper_names_no_kill(self):
        nodes = _helper_nodes()
        # The control: it read the helper's code, not its text.
        assert any(getattr(n, "attr", None) == "AttachConsole" for _o, n in nodes)
        assert _kill_sites(nodes) == set()
        assert _named_enders(nodes) == set()
        strings = [text for _owner, text in _strings(nodes)]
        assert [s for s in strings if re.search(r"(?i)taskkill", s)] == []

    def test_the_helper_reads_nothing_by_a_computed_name(self):
        assert _by_computed_name(_helper_nodes()) == []
        assert _getattr_reads(_helper_nodes()) == []
        assert _dunders(_helper_nodes()) == []

    def test_the_helper_reads_only_the_known_members_of_its_modules(self):
        # sys.modules hands out any module by name, and ctypes._os is os: each
        # module the helper binds is read only as `module.name`, from a closed
        # list, and the one `from` import binds a module read the same way.
        nodes = [node for _owner, node in _helper_nodes()]
        whole, taken = _bindings(nodes)
        assert taken == _HELPER_FROM
        whole |= {name: f"{module}.{name}" for module, name in taken}
        read, bare = _members_read(nodes, whole)
        assert bare == []
        assert read == _HELPER_MEMBERS

    def test_the_helper_reads_only_the_known_members_of_its_kernel32(self):
        # One read of WinDLL, through whatever name ctypes is bound to, called
        # at once and bound once to one name, and that name read only as
        # `k.member[.member...]`: k["x"], `k2 = k`, `W = ctypes.WinDLL`,
        # `import ctypes as c` and a second WinDLL each reach a function the
        # chain scan would not see.
        nodes = [node for _owner, node in _helper_nodes()]
        whole, _taken = _bindings(nodes)
        parent = {c: n for n in nodes for c in ast.iter_child_nodes(n)}
        windll = [
            node
            for node in nodes
            if isinstance(node, ast.Attribute)
            and node.attr == "WinDLL"
            and isinstance(node.value, ast.Name)
            and whole.get(node.value.id) == "ctypes"
        ]
        assert len(windll) == 1
        loader = parent.get(windll[0])
        assert isinstance(loader, ast.Call) and loader.func is windll[0]
        binds = [
            node
            for node in nodes
            if isinstance(node, ast.Assign) and node.value is loader
        ]
        assert len(binds) == 1
        assert len(binds[0].targets) == 1
        assert isinstance(binds[0].targets[0], ast.Name)
        k = binds[0].targets[0].id
        stores = [
            n
            for n in nodes
            if isinstance(n, ast.Name) and n.id == k and not isinstance(n.ctx, ast.Load)
        ]
        assert stores == [binds[0].targets[0]]
        read: set[str] = set()
        bare = []
        for node in nodes:
            if not (
                isinstance(node, ast.Name) and node.id == k and node is not stores[0]
            ):
                continue
            up = parent.get(node)
            if not (isinstance(up, ast.Attribute) and up.value is node):
                bare.append(f"{k} (line {node.lineno})")
                continue
            while isinstance(up, ast.Attribute):
                read.add(up.attr)
                nxt = parent.get(up)
                up = nxt if isinstance(nxt, ast.Attribute) and nxt.value is up else None
        assert bare == []
        assert read == _HELPER_KERNEL32

    def test_the_helper_imports_only_the_known_modules(self):
        assert _imports(_helper_nodes()) == _HELPER_IMPORTS
