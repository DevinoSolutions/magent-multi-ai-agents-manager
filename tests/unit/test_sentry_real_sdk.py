"""magent's REAL ``init_sentry`` against the REAL installed sentry-sdk.

``test_sentry.py`` drives ``init_sentry`` against a fake SDK, which proves
what magent *passes* but not what the SDK *does* with it: a fake
``LoggingIntegration`` accepts any keyword, so sentry-sdk 3.x quietly turning
INFO log lines into breadcrumbs (its ``breadcrumb_level`` defaults to INFO,
where 2.x's ``level=None`` switched breadcrumbs off) was invisible to it. This
module is the other half: the privacy contract -- errors only, nothing but the
error itself leaves the machine -- asserted on the envelope the real client
hands to its transport, on whichever major is installed (the quality job's
locked env resolves 2.x, the unit matrix's plain pip install resolves 3.x).

The scenario runs in a fresh interpreter, not in the test process: the SDK
patches ``logging``/``http.client``/``sys.excepthook`` process-wide, and a
child with a throwaway HOME is the only reset that is complete. Nothing is
ever sent: the child's transport is an in-memory capture, and the DSN points
at the loopback discard port.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("sentry_sdk")

_DSN = "http://k@127.0.0.1:9/1"

INFO_MARK = "INFO-LOG-MARK-55aa"
QUERY_MARK = "QUERY-MARK-7e2d"
LOCAL_MARK = "LOCAL-VAR-MARK-9f3a"
EXC_MARK = "EXC-MESSAGE-MARK-c41b"

# Runs in the child. argv: dsn, out-file. Mirrors the reviewer's probe: an INFO
# log line, an outgoing loopback http request whose URL carries a query, then
# an exception whose frame holds a marker in a local, captured as a
# logged exception, an explicit capture, and a plain ERROR record.
_SCENARIO = f"""
import atexit, json, logging, sys, threading, urllib.request
import http.server
import sentry_sdk
from sentry_sdk.transport import Transport

atexit.register = lambda *_a, **_k: None  # no flush hook outlives the scenario
events = []

class Capture(Transport):
    def __init__(self, options=None):
        try:
            super().__init__(options)
        except TypeError:
            super().__init__()

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            events.append(event)

    def flush(self, *a, **k):
        return None

    def kill(self):
        return None

real_init = sentry_sdk.init
sentry_sdk.init = lambda **kw: real_init(**kw, transport=Capture())

from magent.log import get_logger
from magent.sentry import init_sentry

init_sentry(sys.argv[1])
client = sentry_sdk.get_client()
assert client.is_active(), "init_sentry left the SDK inactive"

log = get_logger("realsdk")
log.info("launching project at C:/Users/x/{INFO_MARK}")

class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *a):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
threading.Thread(target=server.serve_forever, daemon=True).start()
urllib.request.urlopen(
    "http://127.0.0.1:%d/x?project={QUERY_MARK}" % server.server_port
).read()

def _boom(secret_local):
    password_like = secret_local
    raise RuntimeError("{EXC_MARK}")

try:
    _boom("{LOCAL_MARK}")
except RuntimeError as exc:
    log.exception("failed")
    sentry_sdk.capture_exception(exc)
log.error("plain error record")
sentry_sdk.flush(timeout=2)

options = client.options
json.dump(
    {{
        "version": sentry_sdk.VERSION,
        "events": events,
        "max_breadcrumbs": options.get("max_breadcrumbs"),
        "data_collection": options.get("data_collection"),
    }},
    open(sys.argv[2], "w"),
    default=str,
)
"""


def _collects(value: object) -> bool:
    """Whether a resolved ``data_collection`` entry collects anything."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        return bool(value)
    if isinstance(value, dict):
        if "mode" in value:
            return value["mode"] != "off"
        return any(_collects(v) for v in value.values())
    return bool(value)


def _strings(node: object, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


@pytest.fixture(scope="module")
def captured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    home = tmp_path_factory.mktemp("sentry-real-home")
    out: Path = home / "result.json"
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive or "",
        "HOMEPATH": str(home)[len(home.drive) :] if home.drive else str(home),
        "NO_COLOR": "1",
    }
    # The child must not reach any real service or watch the real machine.
    for name in ("MAGENT_SENTRY_DSN", "SENTRY_DSN"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-c", _SCENARIO, _DSN, str(out)],
        env=env,
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(out.read_text(encoding="utf-8"))


class TestTheRealSdkSendsNothingButTheError:
    def test_the_errors_were_captured(self, captured: dict[str, object]) -> None:
        events = captured["events"]
        assert isinstance(events, list)
        # log.exception and log.error; the explicit capture_exception of the
        # same exception is deduplicated by the SDK. If capture were broken
        # every absence below would be vacuously true.
        assert len(events) == 2
        dumped = json.dumps(events)
        assert EXC_MARK in dumped
        assert "_boom" in dumped, "the traceback frames must survive"

    def test_no_breadcrumbs_ride_along(self, captured: dict[str, object]) -> None:
        """INFO log lines (project names, user paths, ssh hosts) and outgoing
        http requests (URL query) must not be attached to an event, on 2.x
        (`level=None`) and 3.x (`breadcrumb_level`) alike."""
        for event in captured["events"]:
            crumbs = event.get("breadcrumbs") or {}
            values = (
                crumbs.get("values", crumbs) if isinstance(crumbs, dict) else crumbs
            )
            assert not values, values

    def test_the_only_marker_in_an_event_is_the_exception_message(
        self, captured: dict[str, object]
    ) -> None:
        hits = {
            (path, mark)
            for event in captured["events"]
            for path, text in _strings(event)
            for mark in (INFO_MARK, QUERY_MARK, LOCAL_MARK)
            if mark in text
        }
        assert not hits, hits

    def test_no_frame_locals_user_ip_or_request(
        self, captured: dict[str, object]
    ) -> None:
        for event in captured["events"]:
            assert "vars" not in json.dumps(event).replace('"vars_', "")
            assert not event.get("user"), event.get("user")
            assert "request" not in event
            assert "ip_address" not in json.dumps(event)

    def test_nothing_the_sdk_collects_by_category_is_left_on(
        self, captured: dict[str, object]
    ) -> None:
        """3.x only (2.x is opted out via ``send_default_pii``): every ``data_collection`` category the installed SDK
        resolves must be off, bar ``frame_context_lines`` (source lines of
        code around a traceback frame -- code, not user data, and sent on 2.x
        too). A category a future SDK adds defaults to ON, so this fails until
        magent's opt-out list is extended: fail closed, loudly, in CI."""
        resolved = captured["data_collection"]
        if not str(captured["version"]).startswith("3.") or not isinstance(
            resolved, dict
        ):
            pytest.skip("magent opts out by category only on sentry-sdk 3.x")
        still_on = {
            key: value
            for key, value in resolved.items()
            if key != "frame_context_lines" and _collects(value)
        }
        assert not still_on, still_on

    def test_the_breadcrumb_buffer_itself_is_closed(
        self, captured: dict[str, object]
    ) -> None:
        # Belt and braces for any breadcrumb source an integration adds later.
        assert captured["max_breadcrumbs"] == 0
