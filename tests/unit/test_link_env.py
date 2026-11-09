"""MAGENT_LINK / MAGENT_LINK_URL / MAGENT_LINK_APP_URL: the link thread's
switch, the relay address, and the app a pairing QR may open.

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

    def test_the_app_defaults_to_app_magent_now(self, monkeypatch):
        assert (
            str(_bare_env(monkeypatch).link_app_url).rstrip("/")
            == "https://app.magent.now"
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

    def test_another_app_is_accepted(self, monkeypatch):
        _bare_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LINK_APP_URL", "https://app.example.com")
        env = MagentEnv(_env_file=None)
        assert str(env.link_app_url).rstrip("/") == "https://app.example.com"

    def test_a_non_url_app_is_refused(self, monkeypatch):
        from pydantic import ValidationError

        _bare_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LINK_APP_URL", "not a url")
        with pytest.raises(ValidationError):
            MagentEnv(_env_file=None)

    def test_app_url_reads_the_setting(self, monkeypatch):
        from magent import env, link

        _bare_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LINK_APP_URL", "https://app.example.com")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.app_url() == "https://app.example.com"


def test_conftest_pins_the_link_thread_off():
    # The autouse _isolate_magent_home fixture ran before this test.
    assert os.environ.get("MAGENT_LINK") == "0"


class TestTheProductionRelayIsUnreachable:
    @pytest.mark.parametrize(
        "host",
        [
            "api.magent.now",
            "API.Magent.Now.",
            b"relay.magent.now",
            b"api.magent.now..",
            "magent.now",
            "api.magent\u3002now",
            "api\uff0emagent\uff0enow",
            "api.magent\uff61now",
        ],
    )
    def test_resolving_it_fails_the_test(self, host):
        import socket

        with pytest.raises(pytest.fail.Exception, match="production relay"):
            socket.getaddrinfo(host, 443)

    def test_other_names_still_resolve(self):
        import socket

        assert socket.getaddrinfo("127.0.0.1", 9)

    def test_the_fail_open_fallback_cannot_dial_it(self, monkeypatch):
        from magent import env, link

        monkeypatch.setenv("MAGENT_NOT_A_THING", "1")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.relay_url() == link.DEFAULT_RELAY_URL
        client = link.RelayClient(link.relay_url(), timeout=2)
        with pytest.raises(pytest.fail.Exception, match="production relay"):
            client.call("GET", "/v1/host/devices")


def test_conftest_pins_the_relay_to_a_dead_loopback(monkeypatch):
    from magent import env, link

    assert os.environ.get("MAGENT_LINK_URL") == "http://127.0.0.1:9"
    monkeypatch.setattr(env, "_cached_env", None)
    assert link.relay_url() == "http://127.0.0.1:9"
