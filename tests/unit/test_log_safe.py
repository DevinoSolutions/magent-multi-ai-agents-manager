"""log_safe keeps request-controlled text on one log line."""

from magent.log import log_safe

BACKSLASH = chr(92)


def test_crlf_cannot_forge_a_second_record():
    out = log_safe("a\r\nINFO forged")
    assert "\n" not in out
    assert "\r" not in out
    assert out == "a" + BACKSLASH + "r" + BACKSLASH + "nINFO forged"


def test_plain_text_is_unchanged():
    assert log_safe("proj-1") == "proj-1"
