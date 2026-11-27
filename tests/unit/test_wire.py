"""wire: the ``/api/v1`` envelope and the code-to-status table, pinned."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from magent import wire


class TestStatusTable:
    """Spec 3.2: every error code maps to exactly one HTTP status."""

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            ("invalid_request", 400),
            ("unauthorized", 401),
            ("forbidden", 403),
            ("not_found", 404),
            ("method_not_allowed", 405),
            ("conflict", 409),
            ("expired", 410),
            ("payload_too_large", 413),
            ("rate_limited", 429),
            ("internal", 500),
            ("unavailable", 503),
            ("timeout", 504),
        ],
    )
    def test_each_code_has_its_status(self, code, status):
        assert wire.STATUS[code] == status

    def test_the_table_is_exactly_the_twelve_codes(self):
        assert len(wire.STATUS) == 12
        assert set(wire.STATUS) == set(wire.ErrorCode.__args__)


@dataclass(frozen=True)
class _Row:
    session: str
    live: bool


class TestEnvelope:
    def test_ok_wraps_data(self):
        assert wire.ok({"a": 1}) == {"ok": True, "data": {"a": 1}}

    def test_ok_flattens_a_dataclass_and_a_list_of_them(self):
        assert wire.ok(_Row("caramel", True)) == {
            "ok": True,
            "data": {"session": "caramel", "live": True},
        }
        assert wire.ok([_Row("a", True), _Row("b", False)]) == {
            "ok": True,
            "data": [{"session": "a", "live": True}, {"session": "b", "live": False}],
        }

    def test_to_wire_leaves_a_dataclass_type_and_plain_values_alone(self):
        assert wire.to_wire(_Row) is _Row
        assert wire.to_wire("text") == "text"
        assert wire.to_wire(None) is None

    def test_error_without_details_has_no_details_key(self):
        assert wire.error("not_found", "no such session") == {
            "ok": False,
            "error": {"code": "not_found", "message": "no such session"},
        }

    def test_empty_details_are_omitted_too(self):
        body = wire.error("conflict", "busy", {})
        assert "details" not in body["error"]

    def test_error_with_details_carries_them(self):
        assert wire.error("conflict", "busy", {"reason": "busy"}) == {
            "ok": False,
            "error": {
                "code": "conflict",
                "message": "busy",
                "details": {"reason": "busy"},
            },
        }


class TestWireError:
    def test_carries_code_message_and_details(self):
        exc = wire.WireError("timeout", "pane slow", {"reason": "pane_timeout"})
        assert (exc.code, exc.message, exc.details) == (
            "timeout",
            "pane slow",
            {"reason": "pane_timeout"},
        )
        assert str(exc) == "pane slow"

    def test_details_default_to_an_empty_dict(self):
        assert wire.WireError("internal", "boom").details == {}

    def test_from_exc_is_the_error_envelope(self):
        exc = wire.WireError("not_found", "gone", {"reason": "not_live"})
        assert wire.from_exc(exc) == wire.error(
            "not_found", "gone", {"reason": "not_live"}
        )
        assert wire.from_exc(wire.WireError("internal", "boom")) == {
            "ok": False,
            "error": {"code": "internal", "message": "boom"},
        }
