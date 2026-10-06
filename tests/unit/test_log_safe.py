"""log_safe keeps request-controlled text on one log line."""

from magent.log import log_safe


def test_crlf_cannot_forge_a_second_record():
    out = log_safe("a
INFO forged")
    assert "
" not in out
    assert "" not in out
    assert out == "a" + chr(92) + "r" + chr(92) + "nINFO forged"


def test_plain_text_is_unchanged():
    assert log_safe("proj-1") == "proj-1"
