import sys

import pytest

from magent.config import WindowConfig
from magent.titles import (
    MAGENT_TITLE_PREFIX,
    generate_titles,
    get_leaf_name,
    make_title,
    parse_title,
)


class TestGetLeafName:
    def test_unix_path(self):
        assert get_leaf_name("/home/user/code/api") == "api"

    def test_windows_path(self):
        assert get_leaf_name("C:\\Users\\user\\code\\api") == "api"

    def test_forward_slashes(self):
        assert get_leaf_name("internal/api") == "api"

    def test_trailing_slash(self):
        assert get_leaf_name("/home/user/api/") == "api"

    def test_simple_name(self):
        assert get_leaf_name("api") == "api"


class TestGenerateTitles:
    def test_no_windows_uses_title(self):
        titles = generate_titles(title="my-api", path="internal/api", windows=None)
        assert titles == ["my-api"]

    def test_no_windows_no_title_uses_leaf(self):
        titles = generate_titles(title=None, path="internal/api", windows=None)
        assert titles == ["api"]

    def test_windows_list_auto_titles(self):
        windows = [WindowConfig(), WindowConfig(), WindowConfig()]
        titles = generate_titles(title=None, path="internal/api", windows=windows)
        assert titles == ["api", "api-2", "api-3"]

    def test_windows_list_with_title(self):
        windows = [WindowConfig(), WindowConfig(), WindowConfig()]
        titles = generate_titles(title="my-api", path="internal/api", windows=windows)
        assert titles == ["my-api", "my-api-2", "my-api-3"]

    def test_single_window_same_as_none(self):
        titles = generate_titles(title=None, path="internal/api", windows=None)
        assert titles == ["api"]

    def test_windows_named_array(self):
        windows = [
            WindowConfig(name="feat"),
            WindowConfig(name="bugs"),
            WindowConfig(name="review"),
        ]
        titles = generate_titles(title=None, path="internal/api", windows=windows)
        assert titles == ["feat", "bugs", "review"]

    def test_windows_named_array_ignores_title(self):
        windows = [WindowConfig(name="a"), WindowConfig(name="b")]
        titles = generate_titles(title="ignored", path="internal/api", windows=windows)
        assert titles == ["a", "b"]

    def test_windows_mixed_named_and_unnamed(self):
        windows = [WindowConfig(name="custom"), WindowConfig()]
        titles = generate_titles(title=None, path="internal/api", windows=windows)
        assert titles == ["custom", "api-2"]

    def test_windows_with_tool_override(self):
        windows = [WindowConfig(tool="codex"), WindowConfig(command="agy --yolo")]
        titles = generate_titles(title=None, path="internal/api", windows=windows)
        assert titles == ["api", "api-2"]


class TestTitleGrammar:
    """make_title/parse_title are the single title grammar every producer
    (launch, attach, the attention badge renderer) and consumer (hotkey,
    tiling) shares. A change to either side must fail loudly here."""

    def test_prefix_value(self):
        assert MAGENT_TITLE_PREFIX == "magent:"

    def test_plain_title_round_trips(self):
        assert make_title("api") == "magent:api"
        assert parse_title("magent:api") == ("api", None)

    @pytest.mark.parametrize(
        ("state", "glyph"),
        [("needs-input", "!"), ("error", "x"), ("done", "+")],
    )
    def test_badged_title_round_trips(self, state, glyph):
        title = make_title("api", state)
        assert title == f"magent:[{glyph}] api"
        assert parse_title(title) == ("api", state)

    def test_quiet_states_render_unbadged(self):
        assert make_title("api", "working") == "magent:api"
        assert make_title("api", "idle") == "magent:api"

    def test_non_md_titles_parse_to_none(self):
        assert parse_title("Windows Terminal") is None
        assert parse_title("api") is None
        assert parse_title("") is None

    def test_unknown_glyph_is_part_of_the_name(self):
        # A newer writer's badge must degrade readably, not vanish.
        assert parse_title("magent:[?] api") == ("[?] api", None)

    def test_hostile_shapes_do_not_crash(self):
        assert parse_title("magent:") == ("", None)
        assert parse_title("magent:[") == ("[", None)
        assert parse_title("magent:[!]") == ("[!]", None)
        assert parse_title("magent:[!] ") == ("", "needs-input")

    def test_prefix_disabled_yields_bare_name(self):
        # windowTitlePrefix=false: titles are the bare project name -- no
        # magent: prefix, and no state badge even when a state is passed.
        assert make_title("api", prefix=False) == "api"
        assert make_title("api", "needs-input", prefix=False) == "api"
        assert make_title("api", "error", prefix=False) == "api"

    def test_prefix_disabled_bare_name_parses_to_none(self):
        # The consumer contract that makes badging/hotkey/tiling degrade safely:
        # a bare title carries no grammar, so parse_title returns None and every
        # grammar-dependent consumer no-ops on it.
        assert parse_title(make_title("api", prefix=False)) is None

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="hotkey is Windows-only (ImportError off-Windows)",
    )
    def test_hotkey_consumes_the_grammar(self):
        from magent.hotkey import project_from_title

        assert project_from_title(make_title("my-project")) == "my-project"
        assert project_from_title(make_title("my-project", "error")) == "my-project"
