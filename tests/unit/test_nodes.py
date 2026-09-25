"""nodes.py -- pure data + policy for running a project on a pool machine."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from dataclasses import MISSING
from pathlib import Path

import pytest

from magent import nodes
from magent.config import (
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)
from magent.nodes import (
    LoadSample,
    LocalGitState,
    Node,
    NodeConfigError,
    NodeMapEntry,
    Recipe,
    RepoSpec,
    node_for_nick,
)
from tests.conftest import REAL_MAGENT_DIR

NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


class TestTheDataShapes:
    def test_a_node_targets_user_at_host(self):
        assert NODE.target == "amin@devino-second"

    @pytest.mark.parametrize(
        "shape",
        [
            NODE,
            RepoSpec(url="u", branch="main", remote_dir="~/magent/x"),
            LocalGitState(
                path=Path("x"),
                url="u",
                branch="main",
                dirty=False,
                unpushed=False,
                detached=False,
            ),
            Recipe(
                project="x",
                sid="x",
                repos=(),
                push_files=(),
                memory_dir=None,
                remote_root="~/magent/x",
            ),
            LoadSample(
                ts=0.0,
                nproc=1,
                load1=0.0,
                load5=0.0,
                load15=0.0,
                mem_total_mb=1,
                mem_avail_mb=1,
                my_sessions=0,
            ),
        ],
    )
    def test_every_shape_is_frozen(self, shape):
        field_name = dataclasses.fields(shape)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(shape, field_name, "changed")

    def test_a_git_state_carries_no_ignored_listing_by_default(self):
        state = LocalGitState(
            path=Path("x"),
            url="u",
            branch="main",
            dirty=False,
            unpushed=False,
            detached=False,
        )
        assert state.ignored == ()

    def test_a_recipe_carries_no_warnings_by_default(self):
        recipe = Recipe(
            project="x",
            sid="x",
            repos=(),
            push_files=(),
            memory_dir=None,
            remote_root="~/magent/x",
        )
        assert recipe.warnings == ()


class TestTheNodeStoreLayout:
    def test_the_store_lives_under_magent_nodes(self):
        # tests/conftest.py has monkeypatched both constants on the LIVE module
        # (they are import-bound), so reading nodes.NODES_DIR here would pin
        # the patch. A fresh, unregistered load of the module re-runs the
        # PRODUCT's own binding -- under the redirected home, so Path.home() is
        # tmp -- without touching sys.modules or spawning an interpreter.
        spec = importlib.util.find_spec("magent.nodes")
        assert spec is not None
        assert spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        store_dir, map_path = fresh.NODES_DIR, fresh.NODE_MAP_PATH
        assert store_dir == Path.home() / ".magent" / "nodes"
        assert map_path == store_dir / "node-map.json"

    @pytest.mark.parametrize("name", ["NODES_DIR", "NODE_MAP_PATH"])
    def test_no_test_can_reach_the_real_store(self, name):
        assert not getattr(nodes, name).is_relative_to(REAL_MAGENT_DIR)


class TestEncodedProjectDir:
    def test_it_delegates_to_the_one_encoder(self, monkeypatch):
        # An inlined copy of the encoder would still match it on any input;
        # only a patched encoder proves the wrapper calls through.
        monkeypatch.setattr(nodes, "encode_claude_project_path", lambda p: "sentinel")
        assert nodes.encoded_project_dir("/anything") == "sentinel"

    def test_a_node_side_path_encodes_by_the_same_rule(self):
        assert (
            nodes.encoded_project_dir("/home/amin/magent/sendly")
            == "-home-amin-magent-sendly"
        )


ENTRY = NodeMapEntry(
    nick="second",
    sid="api",
    placed_ts=1727200000.0,
    attached_existing=False,
    remote_root="/home/amin/magent/api",
)


# The five fields every node-map.json written by the first release carries.
V1_FIELDS = ("nick", "sid", "placed_ts", "attached_existing", "remote_root")


def _entry_text(placed_ts: str) -> str:
    """ENTRY as raw JSON text with ``placed_ts`` spelled literally -- NaN,
    Infinity and a 400-digit int are text json.dumps cannot be asked for."""
    fields = {**dataclasses.asdict(ENTRY), "placed_ts": "@TS@"}
    return json.dumps(fields).replace('"@TS@"', placed_ts)


class _Busy:
    """A stand-in NODE_MAP_PATH whose read_text raises ``errors`` in order,
    then serves ``text`` -- the Windows reader racing an os.replace."""

    def __init__(self, errors: list[OSError], text: str = "{}") -> None:
        self.errors = list(errors)
        self.text = text
        self.reads = 0

    def read_text(self, encoding: str) -> str:
        self.reads += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.text


@pytest.fixture
def node_map(tmp_path, monkeypatch):
    path = tmp_path / "nodes" / "node-map.json"
    monkeypatch.setattr(nodes, "NODE_MAP_PATH", path)
    return path


class TestTheNodeMap:
    def test_what_is_written_reads_back(self, node_map):
        other = dataclasses.replace(
            ENTRY, nick="third", sid="web", attached_existing=True
        )
        nodes.write_node_map({"api": ENTRY, "web": other})
        assert nodes.read_node_map() == {"api": ENTRY, "web": other}

    def test_the_file_is_keyed_by_project_name(self, node_map):
        # The on-disk shape D writes and E/G read -- pinned, not implied.
        nodes.write_node_map({"api": ENTRY})
        assert json.loads(node_map.read_text(encoding="utf-8")) == {
            "api": {
                "nick": "second",
                "sid": "api",
                "placed_ts": 1727200000.0,
                "attached_existing": False,
                "remote_root": "/home/amin/magent/api",
            }
        }

    def test_a_missing_file_is_an_empty_map(self, node_map):
        assert not node_map.exists()
        assert nodes.read_node_map() == {}

    def test_a_torn_file_is_an_empty_map(self, node_map):
        node_map.parent.mkdir(parents=True)
        node_map.write_text('{"api": {"nick": "sec', encoding="utf-8")
        assert nodes.read_node_map() == {}

    @pytest.mark.parametrize("text", ["[1, 2]", '"api"', "null", ""])
    def test_a_file_that_is_not_an_object_is_an_empty_map(self, node_map, text):
        node_map.parent.mkdir(parents=True)
        node_map.write_text(text, encoding="utf-8")
        assert nodes.read_node_map() == {}

    @pytest.mark.parametrize(
        "bad",
        [
            '{"nick": "third"}',
            _entry_text("true"),
            _entry_text("NaN"),
            _entry_text("Infinity"),
            _entry_text("-Infinity"),
            _entry_text("1e400"),
            # float() of a 309+-digit int raises OverflowError, not inf.
            _entry_text("1" + "0" * 400),
        ],
        ids=["partial", "bool-ts", "nan", "inf", "-inf", "1e400", "huge-int"],
    )
    def test_a_malformed_entry_is_dropped_alone(self, node_map, bad):
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            f'{{"api": {_entry_text("1727200000.0")}, "db": {bad}}}',
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}
        assert nodes.load_node_map_strict() == {"api": ENTRY}

    def test_a_hand_written_v1_file_reads_back(self, node_map):
        # Every other read test builds its input from asdict(ENTRY), which
        # moves with the dataclass. This is the first release's file, frozen.
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            '{"api": {"nick": "second", "sid": "api", "placed_ts": 1727200000.0,'
            ' "attached_existing": false, "remote_root": "/home/amin/magent/api"}}',
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}

    def test_every_field_after_v1_has_a_default(self):
        # An older file lacks every later field; without a default it can't load.
        for f in dataclasses.fields(NodeMapEntry):
            if f.name not in V1_FIELDS:
                assert f.default is not MISSING or f.default_factory is not MISSING, (
                    f.name
                )

    def test_an_unknown_field_is_ignored(self, node_map):
        # A newer magent may add a (defaulted) field; an older reader keeps working.
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            json.dumps({"api": {**dataclasses.asdict(ENTRY), "future": 1}}),
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}

    def test_a_non_finite_timestamp_is_never_written(self, node_map):
        nodes.write_node_map({"api": ENTRY})
        bad = dataclasses.replace(ENTRY, placed_ts=float("nan"))
        with pytest.raises(ValueError):
            nodes.write_node_map({"web": bad})
        assert nodes.read_node_map() == {"api": ENTRY}
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_a_write_replaces_the_whole_map(self, node_map):
        nodes.write_node_map({"api": ENTRY})
        nodes.write_node_map({"web": dataclasses.replace(ENTRY, sid="web")})
        assert set(nodes.read_node_map()) == {"web"}

    def test_a_write_leaves_no_temp_file(self, node_map):
        nodes.write_node_map({"api": ENTRY})
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_a_failed_write_keeps_the_old_map(self, node_map, monkeypatch):
        nodes.write_node_map({"api": ENTRY})

        def refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(nodes.os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            nodes.write_node_map({"web": ENTRY})
        # read_node_map never calls os.replace, so the patch doesn't blind it.
        assert nodes.read_node_map() == {"api": ENTRY}
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_two_writes_in_flight_never_share_a_temp_file(self, node_map, monkeypatch):
        # Threads share a pid: a pid-derived temp name let one writer's
        # os.replace move (or clobber) the other's half-written file.
        held: list[Path] = []
        monkeypatch.setattr(
            nodes.os, "replace", lambda src, dst: held.append(Path(src))
        )
        nodes.write_node_map({"api": ENTRY})
        nodes.write_node_map({"web": ENTRY})
        assert len(set(held)) == 2
        assert all(p.exists() and p.parent == node_map.parent for p in held)

    @pytest.mark.parametrize(
        "field", [f.name for f in dataclasses.fields(NodeMapEntry)]
    )
    def test_every_field_is_frozen(self, field):
        # setattr with a parametrized name: no literal attribute, so no
        # suppression comment is needed (DECISION-26 iv).
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(ENTRY, field, "x")


class TestTheStrictRead:
    """load_node_map_strict: ``{}`` means "no file" and nothing else."""

    def test_a_missing_file_is_an_empty_map(self, node_map):
        assert nodes.load_node_map_strict() == {}

    def test_a_torn_file_raises(self, node_map):
        node_map.parent.mkdir(parents=True)
        node_map.write_text('{"api": {"nick": "sec', encoding="utf-8")
        with pytest.raises(ValueError):
            nodes.load_node_map_strict()

    @pytest.mark.parametrize("text", ["[1, 2]", '"api"', "null"])
    def test_a_file_that_is_not_an_object_raises(self, node_map, text):
        node_map.parent.mkdir(parents=True)
        node_map.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="not a JSON object"):
            nodes.load_node_map_strict()

    def test_a_briefly_busy_map_is_retried_not_read_as_empty(self, monkeypatch):
        full = json.dumps({"api": dataclasses.asdict(ENTRY)})
        busy = _Busy([PermissionError(13, "busy")] * 3, text=full)
        sleeps: list[float] = []
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", busy)
        monkeypatch.setattr(nodes.time, "sleep", sleeps.append)
        assert nodes.load_node_map_strict() == {"api": ENTRY}
        assert busy.reads == 4
        assert sleeps == [nodes._BUSY_SLEEP_S] * 3

    def test_a_map_that_stays_busy_raises_after_the_retries(self, monkeypatch):
        busy = _Busy([PermissionError(13, "busy")] * 100)
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", busy)
        monkeypatch.setattr(nodes.time, "sleep", lambda s: None)
        with pytest.raises(PermissionError):
            nodes.load_node_map_strict()
        assert busy.reads == nodes._BUSY_RETRIES + 1

    def test_the_tolerant_read_turns_a_busy_map_into_empty(self, monkeypatch):
        # read_node_map's contract: the map never stops a launch.
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", _Busy([PermissionError()] * 100))
        monkeypatch.setattr(nodes.time, "sleep", lambda s: None)
        assert nodes.read_node_map() == {}

    def test_any_other_os_error_is_not_retried(self, monkeypatch):
        busy = _Busy([OSError(5, "I/O error")])
        sleeps: list[float] = []
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", busy)
        monkeypatch.setattr(nodes.time, "sleep", sleeps.append)
        with pytest.raises(OSError, match="I/O error"):
            nodes.load_node_map_strict()
        assert (busy.reads, sleeps) == (1, [])


def _pool(entries: dict[str, NodeConfig] | None = None) -> MagentConfig:
    """A fresh pool per call: MagentConfig and Settings are plain (mutable)
    dataclasses, so one shared module-level config could carry a test's edit
    into the next. Default: ``second`` (explicit user) and ``third`` (none)."""
    if entries is None:
        entries = {
            "second": NodeConfig(nick="second", host="devino-second", user="amin"),
            "third": NodeConfig(nick="third", host="devino-third"),
        }
    return MagentConfig(projects=[], settings=Settings(nodes=entries))


class TestResolve:
    def test_a_pinned_project_resolves_to_its_node(self):
        node = nodes.resolve(
            _pool(), ProjectConfig(path="api", node="second"), local_user="whoever"
        )
        assert node == Node(
            nick="second", host="devino-second", user="amin", root="~/magent"
        )

    def test_no_configured_user_means_the_local_one_lowercased(self):
        node = nodes.resolve(
            _pool(), ProjectConfig(path="api", node="third"), local_user="Amin"
        )
        assert node.user == "amin"

    def test_auto_resolves_to_the_placed_node(self):
        node = nodes.resolve(
            _pool(),
            ProjectConfig(path="api", node="auto"),
            local_user="amin",
            placed="third",
        )
        assert node.nick == "third"

    def test_a_pinned_project_ignores_a_placement(self):
        node = nodes.resolve(
            _pool(),
            ProjectConfig(path="api", node="second"),
            local_user="amin",
            placed="third",
        )
        assert node.nick == "second"

    def test_auto_without_a_placement_is_refused(self):
        with pytest.raises(NodeConfigError, match="placement"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="auto"), local_user="amin"
            )

    def test_a_cloud_project_is_refused_clearly_not_a_key_error(self):
        # The cloud backend (DECISION-8, Task J) resolves its own projects; a
        # caller that hands one to the node resolver gets a named refusal.
        with pytest.raises(NodeConfigError, match="cloud backend"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="cloud"), local_user="amin"
            )

    def test_a_project_without_a_node_is_refused(self):
        with pytest.raises(NodeConfigError, match="not a node project"):
            nodes.resolve(_pool(), ProjectConfig(path="api"), local_user="amin")

    def test_a_nick_missing_from_the_pool_is_refused_naming_it(self):
        # A PINNED nick falls through to node_for_nick's unknown-nick error,
        # prefixed with the project; only an auto placement says "re-place".
        with pytest.raises(
            NodeConfigError,
            match=r"^api: node 'fourth' is not in settings\.nodes; "
            r"known nodes: second, third$",
        ):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="fourth"), local_user="amin"
            )

    def test_a_stale_placement_is_refused_saying_it_was_a_placement(self):
        # The pool shrank after placement chose a node: the fix is to re-place,
        # not to edit the project's pin (it has none).
        with pytest.raises(
            NodeConfigError,
            match=r"placement chose 'fourth', which is no longer in settings\.nodes;"
            r" re-place \(known nodes: second, third\)",
        ):
            nodes.resolve(
                _pool(),
                ProjectConfig(path="api", node="auto"),
                local_user="amin",
                placed="fourth",
            )

    def test_an_empty_pool_says_there_are_no_known_nodes(self):
        with pytest.raises(NodeConfigError, match="known nodes: none"):
            nodes.resolve(
                _pool({}), ProjectConfig(path="api", node="second"), local_user="amin"
            )

    @pytest.mark.parametrize("local_user", ["root", "ROOT"])
    def test_an_implicit_root_user_is_refused(self, local_user):
        # Lowercasing happens BEFORE the D4 check, so "ROOT" cannot slip past.
        with pytest.raises(NodeConfigError, match="D4"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="third"), local_user=local_user
            )

    @pytest.mark.parametrize(
        ("local_user", "derived"),
        [("Amin Dhouib", "amin dhouib"), (" ", " "), ("1amin", "1amin")],
    )
    def test_a_derived_user_ssh_cannot_log_in_as_is_refused(self, local_user, derived):
        # A Windows USERNAME may hold a space; whitespace passes `if not user`.
        with pytest.raises(NodeConfigError) as err:
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="third"), local_user=local_user
            )
        assert "settings.nodes.third.user" in str(err.value)
        assert repr(local_user) in str(err.value)
        assert repr(derived) in str(err.value)

    def test_an_explicit_user_is_not_second_guessed(self):
        pool = _pool({"sixth": NodeConfig(nick="sixth", host="h", user="Svc.Account")})
        node = nodes.resolve(
            pool, ProjectConfig(path="api", node="sixth"), local_user="Amin Dhouib"
        )
        assert node.user == "Svc.Account"

    def test_an_explicit_root_user_is_honoured(self):
        pool = _pool(
            {"fifth": NodeConfig(nick="fifth", host="devino-fifth", user="root")}
        )
        node = nodes.resolve(
            pool, ProjectConfig(path="api", node="fifth"), local_user="amin"
        )
        assert node.user == "root"

    def test_no_user_anywhere_is_refused(self):
        with pytest.raises(NodeConfigError, match=r"settings\.nodes\.third\.user"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="third"), local_user=""
            )


class TestANickResolvesLikeAProject:
    # What a nick shares with a project is pinned through resolve() in
    # TestResolve; this class pins only what differs when there is no project.

    def test_an_unknown_nick_names_the_pool(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^node 'fifth' is not in settings\.nodes; known nodes: second, third$",
        ):
            node_for_nick(_pool(), "fifth", local_user="amin")

    @pytest.mark.parametrize("nick", ["auto", "cloud"])
    def test_a_placement_word_as_a_nick_is_just_an_unknown_nick(self, nick):
        # Config validation keeps the reserved words out of the pool, so the
        # ordinary refusal is the true one; no special case is wanted.
        with pytest.raises(
            NodeConfigError,
            match=rf"^node '{nick}' is not in settings\.nodes; known nodes: second, third$",
        ):
            node_for_nick(_pool(), nick, local_user="amin")

    def test_the_label_prefixes_the_unknown_nick_error(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^api: node 'fifth' is not in settings\.nodes; known nodes: second, third$",
        ):
            node_for_nick(_pool(), "fifth", local_user="amin", label="api")

    def test_an_empty_pool_says_there_are_no_known_nodes(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^node 'fifth' is not in settings\.nodes; known nodes: none$",
        ):
            node_for_nick(_pool({}), "fifth", local_user="amin")

    @pytest.mark.parametrize(
        ("local_user", "derived"),
        [("Amin Dhouib", "amin dhouib"), (" ", " "), ("1amin", "1amin")],
    )
    def test_a_derived_user_ssh_cannot_log_in_as_is_refused_by_nick(
        self, local_user, derived
    ):
        # The login check lives in node_for_nick, not resolve: a caller holding
        # only a nick (one read from the node map) gets the same refusal.
        with pytest.raises(NodeConfigError) as err:
            node_for_nick(_pool(), "third", local_user=local_user)
        assert "settings.nodes.third.user" in str(err.value)
        assert repr(local_user) in str(err.value)
        assert repr(derived) in str(err.value)

    def test_an_empty_label_is_no_label(self):
        with pytest.raises(NodeConfigError, match=r"^node 'fifth' "):
            node_for_nick(_pool(), "fifth", local_user="amin", label="")

    @pytest.mark.parametrize(
        ("local_user", "expected"),
        [
            ("", r"^settings\.nodes\.third\.user is not set"),
            ("root", r"^settings\.nodes\.third: magent is running as root"),
            (
                "Amin Dhouib",
                (
                    r"^settings\.nodes\.third\.user is not set and the local "
                    r"username 'Amin Dhouib' is not a node login"
                ),
            ),
        ],
    )
    def test_the_label_never_prefixes_a_settings_error(self, local_user, expected):
        # These name settings.nodes.<nick>, the thing to fix; the project that
        # led there is not part of the fix.
        with pytest.raises(NodeConfigError, match=expected):
            node_for_nick(_pool(), "third", local_user=local_user, label="api")


class TestTheMirrorLayout:
    def test_every_path_hangs_off_the_nodes_dir(self, tmp_path):
        assert nodes.node_dir("second", nodes_dir=tmp_path) == tmp_path / "second"
        assert nodes.transcripts_dir("second", "api", nodes_dir=tmp_path) == (
            tmp_path / "second" / "api" / "transcripts"
        )
        assert nodes.state_dir("second", "api", nodes_dir=tmp_path) == (
            tmp_path / "second" / "api" / "state"
        )
        assert (
            nodes.sessions_path("second", nodes_dir=tmp_path)
            == tmp_path / "second" / "sessions.json"
        )
        assert (
            nodes.load_path("second", nodes_dir=tmp_path)
            == tmp_path / "second" / "load.jsonl"
        )
        assert (
            nodes.pull_marks_path("second", nodes_dir=tmp_path)
            == tmp_path / "second" / "pull.json"
        )

    def test_the_default_root_is_read_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path)
        assert nodes.state_dir("second", "api") == tmp_path / "second" / "api" / "state"


class TestAtomicWrites:
    def test_a_write_lands_whole_with_no_temp_left(self, tmp_path):
        target = tmp_path / "a" / "b.json"
        nodes.write_json_atomic(target, {"x": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
        assert [p.name for p in target.parent.iterdir()] == ["b.json"]

    def test_a_failed_write_keeps_the_old_file_and_no_temp(self, tmp_path, monkeypatch):
        target = tmp_path / "b.json"
        nodes.write_text_atomic(target, "old\n")

        def refuse(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(nodes.os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            nodes.write_text_atomic(target, "new\n")
        assert target.read_text(encoding="utf-8") == "old\n"
        assert [p.name for p in tmp_path.iterdir()] == ["b.json"]

    def test_the_temp_file_is_never_a_json_a_reader_could_glob(
        self, tmp_path, monkeypatch
    ):
        # Readers glob `*.json` in a mirror dir; a half-written temp must never
        # match. It is a sibling (os.replace is atomic only within one fs).
        seen: list[Path] = []
        real_replace = nodes.os.replace

        def spy(src, dst):
            seen.append(Path(src))
            real_replace(src, dst)

        monkeypatch.setattr(nodes.os, "replace", spy)
        target = tmp_path / "b.json"
        nodes.write_json_atomic(target, {"x": 1})
        nodes.write_json_atomic(target, {"x": 2})
        assert len(seen) == 2
        assert all(p.parent == tmp_path for p in seen)
        assert all(p.suffix == ".tmp" and not p.name.endswith(".json") for p in seen)
        assert seen[0] != seen[1]

    def test_a_failing_fdopen_closes_the_descriptor_and_leaves_no_temp(
        self, tmp_path, monkeypatch
    ):
        # Between mkstemp and fdopen the raw fd is ours alone: if fdopen raises
        # it must be closed here, or it leaks (and on Windows the open handle
        # would also make the temp's unlink fail, leaving the temp behind).
        made: list[int] = []
        real_mkstemp = nodes.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            made.append(fd)
            return fd, name

        def broken_fdopen(*_args, **_kwargs):
            raise MemoryError("no buffer")

        monkeypatch.setattr(nodes.tempfile, "mkstemp", recording_mkstemp)
        monkeypatch.setattr(nodes.os, "fdopen", broken_fdopen)
        target = tmp_path / "b.json"
        with pytest.raises(MemoryError, match="no buffer"):
            nodes.write_text_atomic(target, "new\n")
        assert len(made) == 1
        with pytest.raises(OSError):
            os.fstat(made[0])
        assert list(tmp_path.iterdir()) == []

    def test_a_non_finite_number_is_refused_before_anything_is_written(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        with pytest.raises(ValueError, match="JSON compliant"):
            nodes.write_json_atomic(sub / "b.json", {"ts": float("nan")})
        # Not only the target: no temp file either, in the dir it would use.
        assert list(sub.iterdir()) == []


class TestTheSessionsSnapshot:
    def test_a_written_snapshot_reads_back(self, tmp_path):
        nodes.write_json_atomic(
            nodes.sessions_path("second", nodes_dir=tmp_path),
            {"ts": 5.0, "sessions": ["api", "web"]},
        )
        assert nodes.read_sessions("second", nodes_dir=tmp_path) == nodes.NodeSessions(
            ts=5.0, sessions=("api", "web")
        )

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "{torn",
            "[]",
            '{"ts": true, "sessions": []}',
            '{"ts": 1, "sessions": "api"}',
            # A non-finite ts would make sessions_stale() answer "fresh" forever.
            '{"ts": NaN, "sessions": []}',
            '{"ts": Infinity, "sessions": []}',
            # Any non-string entry is corruption, not a name to skip.
            '{"ts": 1, "sessions": ["a", 3, null]}',
            '{"ts": 1, "sessions": ["a", ["b"]]}',
        ],
    )
    def test_an_unusable_snapshot_reads_as_none(self, tmp_path, text):
        path = nodes.sessions_path("second", nodes_dir=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        assert nodes.read_sessions("second", nodes_dir=tmp_path) is None

    def test_a_missing_snapshot_reads_as_none(self, tmp_path):
        assert nodes.read_sessions("second", nodes_dir=tmp_path) is None

    def test_a_snapshot_is_stale_after_two_pull_intervals(self):
        snap = nodes.NodeSessions(ts=100.0, sessions=())
        assert not nodes.sessions_stale(snap, pull_interval_s=30, now=160.0)
        assert nodes.sessions_stale(snap, pull_interval_s=30, now=160.5)
        assert nodes.sessions_stale(None, pull_interval_s=30, now=0.0)

    def test_a_snapshot_from_the_future_reads_fresh_and_never_raises(self):
        # A backwards wall-clock jump: the age goes negative, which is fresh.
        snap = nodes.NodeSessions(ts=1_000.0, sessions=("api",))
        assert not nodes.sessions_stale(snap, pull_interval_s=30, now=10.0)
        assert nodes.sessions_stale(snap, pull_interval_s=30, now=1_060.5)
