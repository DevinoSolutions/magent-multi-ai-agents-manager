"""MAGENT_LINK / MAGENT_LINK_URL: the link thread's switch and relay address.

MAGENT_LINK joins the test-isolation law: conftest pins it to 0 for every
tier, because a test that starts a real serve on a linked developer box would
otherwise dial the real relay with the developer's own host token.
"""

from __future__ import annotations

import os

import pytest

from magent.env import MagentEnv


def _bare_env(monkeypatch: pytest.MonkeyPatch) -> MagentEnv:
    """MagentEnv from an environment with no MAGENT_* keys and no dotenv."""
    for key in list(os.environ):
        if key.upper().startswith("MAGENT_"):
            monkeypatch.delenv(key, raising=False)
    return MagentEnv(_env_file=None)


class TestDefaults:
    def test_the_link_thread_is_on_by_default(self, monkeypatch):
        assert _bare_env(monkeypatch).link is True

    def test_the_relay_defaults_to_api_magent_now(self, monkeypatch):
        assert (
            str(_bare_env(monkeypatch).link_url).rstrip("/") == "https://api.magent.now"
        )


class TestOverrides:
    def test_zero_turns_the_thread_off(self, monkeypatch):
        _bare_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LINK", "0")
        assert MagentEnv(_env_file=None).link is False

    def test_another_relay_is_accepted(self, monkeypatch):
        _bare_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LINK_URL", "https://relay.example.com")
        env = MagentEnv(_env_file=None)
        assert str(env.link_url).rstrip("/") == "https://relay.example.com"

    def test_a_non_url_is_refused(self, monkeypatch):
        from pydantic import ValidationError

        _bare_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LINK_URL", "not a url")
        with pytest.raises(ValidationError):
            MagentEnv(_env_file=None)


def test_conftest_pins_the_link_thread_off():
    # The autouse _isolate_magent_home fixture ran before this test.
    assert os.environ.get("MAGENT_LINK") == "0"
