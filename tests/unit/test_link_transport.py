"""link.py transport: envelope decoding, SSE parsing, backoff, seen ring."""

from __future__ import annotations

import datetime
import http.client
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from magent import env, link
from magent.link import (
    Backoff,
    RelayClient,
    RelayError,
    Revoked,
    SeenRing,
    SseEvent,
    TooOld,
    iter_sse,
)


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []

    def log_message(self, format, *args):  # noqa: A002  # reason: BaseHTTPRequestHandler's own signature
        return

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str = "application/json",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _Handler.seen.append(
            (self.command, self.path, dict(self.headers.items()), body)
        )
        replies = {
            "/ok": (
                200,
                {"ok": True, "data": {"echo": json.loads(body) if body else None}},
            ),
            "/missing": (
                404,
                {
                    "ok": False,
                    "error": {"code": "not_found", "message": "No such host."},
                },
            ),
            "/revoked": (
                401,
                {"ok": False, "error": {"code": "unauthorized", "message": "Revoked."}},
            ),
            "/old": (
                426,
                {
                    "ok": False,
                    "error": {"code": "upgrade_required", "message": "Upgrade magent."},
                },
            ),
            "/list": (200, {"ok": True, "data": [1, 2]}),
        }
        if self.path == "/notjson":
            return self._send(200, b"<html>", "text/html")
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b': ok\n\nid: 7\nevent: command\ndata: {"seq": 7}\n\n: ping\n\nevent: devices\ndata: {}\n\n'
            )
            return None
        if self.path == "/big":
            # A valid envelope, 2000 bytes: only the size check refuses it.
            pad = "x" * (2000 - len('{"ok": true, "data": {"pad": ""}}'))
            return self._send(
                200, json.dumps({"ok": True, "data": {"pad": pad}}).encode()
            )
        if self.path == "/big-hang":
            # 2000 bytes now, then silence under a Content-Length promising far
            # more: a bounded read returns at once, an unbounded one waits.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000000")
            self.end_headers()
            self.wfile.write(b"x" * 2000)
            self.wfile.flush()
            time.sleep(1.5)
            return None
        if self.path in ("/drip", "/drip-open"):
            # A byte every 0.1 s: never silent for a whole timeout, so only a
            # deadline on the whole call can end it early. -open has no
            # Content-Length, so the cut reads as an early end of the body.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if self.path == "/drip":
                self.send_header("Content-Length", "40")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
            return None
        if self.path == "/created":
            return self._send(201, b'{"ok": true, "data": {}}')
        if self.path == "/sse-big":
            # 20 data lines of 100 bytes, each far under the line cap, in ONE
            # event: only the per-event cap can refuse it.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"".join([b"data: " + b"y" * 100 + b"\n"] * 20) + b"\n")
            return None
        if self.path == "/redirect":
            # An envelope body too, so only the 3xx check can make it invalid.
            moved = {"ok": False, "error": {"code": "moved", "message": "Moved."}}
            return self._send(
                302,
                json.dumps(moved).encode(),
                extra={"Location": "http://other-host/ok"},
            )
        if self.path == "/sup":
            limited = {"ok": False, "error": {"code": "rate_limited", "message": ""}}
            return self._send(
                429, json.dumps(limited).encode(), extra={"Retry-After": "\u00b2"}
            )
        if self.path == "/sse-json":
            return self._send(200, b'{"ok": true, "data": {}}')
        if self.path in ("/sse-long", "/sse-edge"):
            # -long: one line past MAX_SSE_LINE; -edge: exactly MAX_SSE_LINE
            # bytes with its newline, which is still allowed.
            pad = link.MAX_SSE_LINE - len(b"data: \n")
            pad += 1 if self.path == "/sse-long" else 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"data: " + b"x" * pad + b"\n\n")
            return None
        if self.path.startswith("/busy/"):
            # /busy/<n>/<retry-after>: 429 for the first n hits, then 200.
            _, _, n, retry_after = self.path.split("/")
            if sum(1 for hit in _Handler.seen if hit[1] == self.path) <= int(n):
                limited = {
                    "ok": False,
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests; slow down.",
                    },
                }
                extra = {} if retry_after == "none" else {"Retry-After": retry_after}
                return self._send(429, json.dumps(limited).encode(), extra=extra)
            return self._send(
                200, json.dumps({"ok": True, "data": {"done": True}}).encode()
            )
        if self.path.startswith("/blob"):
            return self._send(200, b"x" * 300_000, "application/octet-stream")
        status, payload = replies.get(
            self.path,
            (500, {"ok": False, "error": {"code": "internal", "message": "boom"}}),
        )
        return self._send(status, json.dumps(payload).encode())

    do_GET = do_POST = do_PUT = do_DELETE = _route


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


class TestCall:
    def test_ok_returns_data_and_sends_the_protocol_and_bearer(self, server):
        data = RelayClient(server, token="mlh_DECOY_t").call("POST", "/ok", {"a": 1})
        assert data == {"echo": {"a": 1}}
        method, path, headers, _ = _Handler.seen[-1]
        assert (method, path) == ("POST", "/ok")
        assert headers["X-Magent-Link"] == "1"
        assert headers["Authorization"] == "Bearer mlh_DECOY_t"
        assert headers["Content-Type"] == "application/json"
        assert headers["User-Agent"].startswith("magent/")

    def test_a_per_call_token_wins(self, server):
        RelayClient(server, token="mlh_DECOY_a").call("GET", "/ok", token="mlp_DECOY_b")
        assert _Handler.seen[-1][2]["Authorization"] == "Bearer mlp_DECOY_b"

    def test_no_token_sends_no_authorization(self, server):
        RelayClient(server).call("GET", "/ok")
        assert "Authorization" not in _Handler.seen[-1][2]

    def test_an_error_envelope_raises_its_code(self, server):
        with pytest.raises(RelayError) as exc:
            RelayClient(server).call("GET", "/missing")
        assert (exc.value.status, exc.value.code, exc.value.message) == (
            404,
            "not_found",
            "No such host.",
        )

    def test_401_is_revoked_and_426_is_too_old(self, server):
        with pytest.raises(Revoked):
            RelayClient(server).call("GET", "/revoked")
        with pytest.raises(TooOld):
            RelayClient(server).call("GET", "/old")

    @pytest.mark.parametrize("path", ["/notjson", "/list"])
    def test_a_non_envelope_is_invalid(self, server, path):
        with pytest.raises(RelayError) as exc:
            RelayClient(server).call("GET", path)
        assert exc.value.code == "invalid_response"

    def test_a_dead_relay_is_a_network_error(self):
        with pytest.raises(RelayError) as exc:
            RelayClient("http://127.0.0.1:9", timeout=2).call("GET", "/ok")
        assert (exc.value.status, exc.value.code) == (0, "network")

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://user:DECOY_secret@relay",
            "http://user:DECOY_secret@relay:99999",
            "http://user:DECOY_secret@relay:DECOY",
        ],
        ids=["scheme", "port-range", "port-text"],
    )
    def test_a_bad_url_is_refused_without_echoing_it(self, url):
        # A bad port is refused here, not at the first call, so a hand-edited
        # link.json is a bad file (reported once), not a crash per retry.
        with pytest.raises(ValueError) as exc:
            RelayClient(url)
        assert "DECOY" not in str(exc.value)


class TestRateLimit:
    def _hits(self, path: str) -> int:
        return sum(1 for hit in _Handler.seen if hit[1] == path)

    def test_a_429_is_retried_after_its_retry_after(self, server):
        slept: list[float] = []
        data = RelayClient(server, sleep=slept.append).call(
            "POST", "/busy/1/2", {"a": 1}
        )
        assert data == {"done": True}
        assert slept == [2.0]
        assert self._hits("/busy/1/2") == 2

    def test_a_stopped_sleep_raises_the_429_instead_of_retrying(self, server):
        # The runner's stop-aware sleep returns True once it is stopping: the
        # 429 stands, and nothing is retried back to back.
        slept: list[float] = []

        def stopped(seconds: float) -> bool:
            slept.append(seconds)
            return True

        with pytest.raises(RelayError) as exc:
            RelayClient(server, sleep=stopped).call("POST", "/busy/1/2", {"a": 1})
        assert (exc.value.status, exc.value.retry_after) == (429, 2.0)
        assert slept == [2.0]
        assert self._hits("/busy/1/2") == 1

    def test_a_429_without_retry_after_backs_off_with_jitter(self, server):
        slept: list[float] = []
        RelayClient(server, sleep=slept.append).call("GET", "/busy/2/none")
        assert len(slept) == 2
        assert 0 <= slept[0] <= 1 and 0 <= slept[1] <= 2

    def test_a_retry_after_at_the_cap_is_waited_out(self, server):
        slept: list[float] = []
        RelayClient(server, sleep=slept.append).call("GET", "/busy/1/60")
        assert slept == [link.RATE_LIMIT_CAP_S]

    def test_a_retry_after_past_the_cap_raises_at_once(self, server):
        slept: list[float] = []
        with pytest.raises(RelayError) as exc:
            RelayClient(server, sleep=slept.append).call("GET", "/busy/1/3600")
        assert (exc.value.status, exc.value.retry_after) == (429, 3600.0)
        assert slept == []
        assert self._hits("/busy/1/3600") == 1

    def test_a_non_ascii_digit_retry_after_is_ignored(self, server):
        # "\u00b2".isdigit() is True, and float() of it is a bare ValueError.
        slept: list[float] = []
        with pytest.raises(RelayError) as exc:
            RelayClient(server, sleep=slept.append, retries=1).call("GET", "/sup")
        assert (exc.value.status, exc.value.retry_after) == (429, None)
        assert len(slept) == 1 and 0 <= slept[0] <= 1

    def test_the_retries_run_out_with_the_last_retry_after(self, server):
        slept: list[float] = []
        with pytest.raises(RelayError) as exc:
            RelayClient(server, sleep=slept.append, retries=2).call("GET", "/busy/9/1")
        assert (exc.value.status, exc.value.code, exc.value.retry_after) == (
            429,
            "rate_limited",
            1.0,
        )
        assert slept == [1.0, 1.0]
        assert self._hits("/busy/9/1") == 3

    def test_a_per_call_retries_override_wins(self, server):
        slept: list[float] = []
        client = RelayClient(server, sleep=slept.append, retries=3)
        with pytest.raises(RelayError) as exc:
            client.call("GET", "/busy/9/1", retries=0)
        assert (exc.value.status, exc.value.retry_after) == (429, 1.0)
        assert slept == [] and self._hits("/busy/9/1") == 1

    def test_a_rate_limited_stream_raises_with_retry_after_and_no_sleep(self, server):
        slept: list[float] = []
        with pytest.raises(RelayError) as exc:
            list(RelayClient(server, sleep=slept.append).stream("/busy/9/5"))
        assert (exc.value.status, exc.value.code, exc.value.retry_after) == (
            429,
            "rate_limited",
            5.0,
        )
        assert slept == []

    def test_other_errors_carry_no_retry_after_and_are_not_retried(self, server):
        slept: list[float] = []
        with pytest.raises(RelayError) as exc:
            RelayClient(server, sleep=slept.append).call("GET", "/missing")
        assert exc.value.retry_after is None
        assert slept == []


def _assert_no_decoy(exc: BaseException) -> None:
    """No fake secret in the error, nor in anything it was raised from."""
    seen: BaseException | None = exc
    while seen is not None:
        assert "DECOY" not in str(seen), repr(seen)
        assert "DECOY" not in repr(seen), repr(seen)
        seen = seen.__cause__ or seen.__context__


class TestSecurity:
    def test_https_verifies_the_certificate_and_the_hostname(self):
        client = RelayClient("https://relay.example.com")
        conn, target = client._connection("https://relay.example.com/v1/x?a=1", 5)
        assert isinstance(conn, http.client.HTTPSConnection)
        assert target == "/v1/x?a=1"
        assert conn._context.verify_mode == ssl.CERT_REQUIRED
        assert conn._context.check_hostname is True

    def test_a_redirect_is_refused_and_not_followed(self, server):
        with pytest.raises(RelayError) as exc:
            RelayClient(server, token="mlh_DECOY_t").call("GET", "/redirect")
        assert (exc.value.status, exc.value.code) == (302, "invalid_response")
        assert [hit[1] for hit in _Handler.seen] == ["/redirect"]

    def test_a_redirected_stream_is_refused(self, server):
        with pytest.raises(RelayError) as exc:
            list(RelayClient(server).stream("/redirect"))
        assert (exc.value.status, exc.value.code) == (302, "invalid_response")

    def test_the_token_never_reaches_an_error(self, server):
        dead = RelayClient("http://127.0.0.1:9", token="mlh_DECOY_t", timeout=2)
        with pytest.raises(RelayError) as exc:
            dead.call("GET", "/ok")
        _assert_no_decoy(exc.value)
        live = RelayClient(server, token="mlh_DECOY_t")
        for path in ("/revoked", "/missing"):
            with pytest.raises(RelayError) as exc:
                list(live.stream(path))
            _assert_no_decoy(exc.value)
        with pytest.raises(RelayError) as exc:
            live.get_bytes(
                f"{server}/missing?sig=DECOY_s", lambda b: None, max_bytes=10
            )
        _assert_no_decoy(exc.value)
        with pytest.raises(RelayError) as exc:
            dead.get_bytes(
                "http://127.0.0.1:9/x?sig=DECOY_s", lambda b: None, max_bytes=10
            )
        _assert_no_decoy(exc.value)
        # A 2xx that is not 200 is a refused stream even with an ok envelope.
        with pytest.raises(RelayError) as exc:
            list(live.stream("/created"))
        assert (exc.value.status, exc.value.code) == (201, "stream_refused")
        _assert_no_decoy(exc.value)

    @pytest.mark.parametrize("scheme", ["http", "https"])
    def test_a_connection_carries_the_timeout(self, scheme):
        client = RelayClient(f"{scheme}://relay.example.com", timeout=30)
        conn, _ = client._connection(f"{scheme}://relay.example.com/v1/x", 7.5)
        assert conn.timeout == 7.5

    @pytest.mark.parametrize(
        "token", ["mlh_DECOY\r\nX-Evil: 1", "mlh_DECOY t", "mlh_DECOY_\u00e9"]
    )
    def test_a_token_that_is_not_printable_ascii_is_refused_unsent(self, server, token):
        with pytest.raises(ValueError, match="bad token") as exc:
            RelayClient(server, token=token).call("GET", "/ok")
        _assert_no_decoy(exc.value)
        with pytest.raises(ValueError, match="bad token"):
            RelayClient(server).call("GET", "/ok", token=token)
        with pytest.raises(ValueError, match="bad token"):
            list(RelayClient(server, token=token).stream("/sse"))
        assert _Handler.seen == []

    @pytest.mark.parametrize("url", ["ftp://relay/x", "file:///etc/passwd", "/blob"])
    def test_get_bytes_fetches_http_and_https_only(self, server, url):
        with pytest.raises(RelayError) as exc:
            RelayClient(server).get_bytes(url, lambda b: None, max_bytes=10)
        assert (exc.value.status, exc.value.code) == (0, "invalid_response")
        assert _Handler.seen == []


class TestStreamAndDownload:
    def test_stream_yields_events_and_skips_comments(self, server):
        events = list(RelayClient(server).stream("/sse", read_timeout=5))
        assert events == [
            SseEvent("command", '{"seq": 7}', "7"),
            SseEvent("devices", "{}", None),
        ]
        assert _Handler.seen[-1][2]["Accept"] == "text/event-stream"

    def test_a_refused_stream_raises_like_a_call(self, server):
        with pytest.raises(Revoked):
            list(RelayClient(server).stream("/revoked"))

    def test_get_bytes_streams_to_the_sink_without_relay_headers(self, server):
        got = bytearray()
        n = RelayClient(server, token="mlh_DECOY_t").get_bytes(
            f"{server}/blob?sig=1", got.extend, max_bytes=1_000_000
        )
        assert n == 300_000 and bytes(got) == b"x" * 300_000
        headers = _Handler.seen[-1][2]
        assert "Authorization" not in headers and "X-Magent-Link" not in headers

    def test_get_bytes_enforces_the_cap(self, server):
        got = bytearray()
        with pytest.raises(RelayError) as exc:
            RelayClient(server).get_bytes(f"{server}/blob", got.extend, max_bytes=1000)
        assert (exc.value.status, exc.value.code) == (413, "payload_too_large")
        assert got == b""  # nothing past the cap ever reaches the sink

    @pytest.mark.parametrize("path", ["/drip", "/drip-open"])
    def test_a_dripping_download_is_cut_at_its_deadline(self, server, path):
        # The deadline bounds the whole download, not each read. -open has no
        # Content-Length, so on Windows the cut reads as an early end of the
        # body: only the expired check tells it from a finished download.
        started = time.monotonic()
        with pytest.raises(RelayError) as exc:
            RelayClient(server, timeout=5).get_bytes(
                f"{server}{path}", lambda b: None, max_bytes=1000, deadline=0.5
            )
        assert (exc.value.status, exc.value.code) == (0, "timeout")
        assert time.monotonic() - started < 2.5

    def test_a_download_that_ends_once_cut_is_a_timeout(self, server, monkeypatch):
        # Where a cut reads as a clean end of an open-ended body (Linux), the
        # loop ends as if the download were whole: the expired check after it
        # is all that stands between a truncated file and a "complete" one.
        # A deadline already expired, that cuts nothing, plays that on every OS.
        class _Expired:
            expired = True

            passed = True

            def __init__(self, sock, seconds):
                pass

            def watch(self, sock):
                pass

            def left(self):
                return 0.0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(link, "_Deadline", _Expired)
        with pytest.raises(RelayError) as exc:
            RelayClient(server).get_bytes(
                f"{server}/blob", lambda b: None, max_bytes=1_000_000
            )
        assert (exc.value.status, exc.value.code) == (0, "timeout")


class TestDownloadTargets:
    """A relay on https hands out presigned https URLs on a public storage
    host, on port 443; the download refuses anything else, before it
    connects."""

    @pytest.fixture
    def no_connect(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("get_bytes connected to a refused target")

        monkeypatch.setattr(link.socket, "create_connection", refuse)

    @staticmethod
    def _resolve_to(monkeypatch, *addresses: str | tuple[str, int]) -> None:
        """getaddrinfo answers with these, shaped as it shapes them: an IPv6
        sockaddr is (host, port, flowinfo, scope_id); a (host, scope) pair
        sets the scope id."""
        infos = []
        for entry in addresses:
            host, scope = entry if isinstance(entry, tuple) else (entry, 0)
            if ":" in host:
                infos.append((socket.AF_INET6, 1, 6, "", (host, 443, 0, scope)))
            else:
                infos.append((socket.AF_INET, 1, 6, "", (host, 443)))
        monkeypatch.setattr(link.socket, "getaddrinfo", lambda *a, **k: infos)

    @staticmethod
    def _dial_log(monkeypatch) -> list[tuple[object, ...]]:
        dialled: list[tuple[object, ...]] = []

        def dial(address, *args, **kwargs):
            dialled.append(address)
            raise OSError("no network in tests")

        monkeypatch.setattr(link.socket, "create_connection", dial)
        return dialled

    @pytest.mark.parametrize(
        "url",
        [
            "http://storage.example.com/o?sig=DECOY_s",
            "https://storage.example.com:8443/o?sig=DECOY_s",
            "https://storage.example.com:99999/o?sig=DECOY_s",
            "https://storage.example.com:port/o?sig=DECOY_s",
            "https://127.0.0.1/o?sig=DECOY_s",
            "https://localhost/o?sig=DECOY_s",
            "https://10.1.2.3/o?sig=DECOY_s",
            "https://192.168.0.7/o?sig=DECOY_s",
            "https://172.16.5.4/o?sig=DECOY_s",
            "https://169.254.169.254/latest?sig=DECOY_s",
            "https://100.64.0.1/o?sig=DECOY_s",
            "https://0.0.0.0/o?sig=DECOY_s",
            "https://[::1]/o?sig=DECOY_s",
            "https://[fd00::1]/o?sig=DECOY_s",
            "https://[fe80::1]/o?sig=DECOY_s",
            "https://[::ffff:127.0.0.1]/o?sig=DECOY_s",
        ],
    )
    def test_an_https_relay_refuses_plain_http_other_ports_and_private_hosts(
        self, no_connect, url
    ):
        with pytest.raises(RelayError) as exc:
            RelayClient("https://relay.example.com").get_bytes(
                url, lambda b: None, max_bytes=10
            )
        assert (exc.value.status, exc.value.code) == (0, "invalid_response")
        _assert_no_decoy(exc.value)

    @pytest.mark.parametrize(
        "address",
        [
            # NAT64 64:ff9b::/96 of a private IPv4.
            pytest.param("64:ff9b::a00:1", id="nat64-10.0.0.1"),
            pytest.param("64:ff9b::7f00:1", id="nat64-127.0.0.1"),
            pytest.param("64:ff9b::a9fe:a9fe", id="nat64-169.254.169.254"),
            # IPv4-mapped ::ffff:0:0/96.
            pytest.param("::ffff:10.0.0.1", id="mapped-10.0.0.1"),
            pytest.param("::ffff:127.0.0.1", id="mapped-127.0.0.1"),
            pytest.param("::ffff:169.254.169.254", id="mapped-169.254.169.254"),
            # IPv4-translated ::ffff:0:a.b.c.d.
            pytest.param("::ffff:0:a00:1", id="translated-10.0.0.1"),
            # The deprecated IPv4-compatible ::a.b.c.d.
            pytest.param("::10.0.0.1", id="compatible-10.0.0.1"),
            pytest.param("::127.0.0.1", id="compatible-127.0.0.1"),
            pytest.param("::7f00:1", id="compatible-hex-127.0.0.1"),
            # 6to4 of a private IPv4.
            pytest.param("2002:a00:1::1", id="6to4-10.0.0.1"),
            # IPv6 link-local, bare and with a scope (in the text, as some
            # platforms give it, and in the sockaddr only, as Windows does).
            pytest.param("fe80::1", id="link-local"),
            pytest.param("fe80::1%1", id="link-local-scope-in-text"),
            pytest.param(("fe80::1", 1), id="link-local-scope-in-sockaddr"),
            # Unique local fc00::/7.
            pytest.param("fc00::1", id="ula-fc00"),
            pytest.param("fd12:3456::1", id="ula-fd12"),
            # Loopback and unspecified.
            pytest.param("::1", id="loopback"),
            pytest.param("::", id="unspecified"),
            # Multicast.
            pytest.param("ff02::1", id="multicast-ff02::1"),
            # Global scope: is_global, so only the multicast check refuses it.
            pytest.param("ff0e::1", id="multicast-ff0e::1"),
            pytest.param("239.255.255.250", id="multicast-239.255.255.250"),
        ],
    )
    def test_an_address_that_reaches_a_private_one_is_refused(
        self, monkeypatch, no_connect, address
    ):
        self._resolve_to(monkeypatch, address)
        with pytest.raises(RelayError) as exc:
            RelayClient("https://relay.example.com").get_bytes(
                "https://storage.example.com/o", lambda b: None, max_bytes=10
            )
        assert (exc.value.status, exc.value.code) == (0, "invalid_response")

    def test_a_name_with_any_private_address_is_refused(self, monkeypatch, no_connect):
        self._resolve_to(monkeypatch, "93.184.216.34", "10.0.0.5")
        with pytest.raises(RelayError) as exc:
            RelayClient("https://relay.example.com").get_bytes(
                "https://storage.example.com/o", lambda b: None, max_bytes=10
            )
        assert (exc.value.status, exc.value.code) == (0, "invalid_response")

    @pytest.mark.parametrize(
        ("address", "url"),
        [
            ("93.184.216.34", "https://storage.example.com/o?sig=DECOY_s"),
            ("93.184.216.34", "https://storage.example.com:443/o?sig=DECOY_s"),
            ("2606:4700::1111", "https://storage.example.com/o?sig=DECOY_s"),
            # NAT64 of 93.184.216.34.
            ("64:ff9b::5db8:d822", "https://storage.example.com/o?sig=DECOY_s"),
            # Judged as the IPv4 address they carry, so a public one passes.
            ("::ffff:93.184.216.34", "https://storage.example.com/o?sig=DECOY_s"),
            ("2002:5db8:d822::1", "https://storage.example.com/o?sig=DECOY_s"),
        ],
    )
    def test_a_public_host_is_dialled_at_the_address_that_was_checked(
        self, monkeypatch, address, url
    ):
        self._resolve_to(monkeypatch, address)
        dialled = self._dial_log(monkeypatch)
        with pytest.raises(RelayError) as exc:
            RelayClient("https://relay.example.com").get_bytes(
                url, lambda b: None, max_bytes=10
            )
        assert (exc.value.status, exc.value.code) == (0, "network")
        assert dialled == [(address, 443)]
        _assert_no_decoy(exc.value)

    def test_an_http_relay_may_download_from_loopback(self, server):
        got = bytearray()
        assert (
            RelayClient(server).get_bytes(f"{server}/blob", got.extend, max_bytes=10**6)
            == 300_000
        )

    @pytest.mark.parametrize(
        "url", ["http://127.0.0.1:99999/o?sig=DECOY_s", "http://127.0.0.1:x/o"]
    )
    def test_a_bad_port_is_refused_as_a_url_not_allowed(self, server, url):
        with pytest.raises(RelayError) as exc:
            RelayClient(server).get_bytes(url, lambda b: None, max_bytes=10)
        assert (exc.value.status, exc.value.code) == (0, "invalid_response")
        _assert_no_decoy(exc.value)


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    """A one-shot-per-connection TLS server on loopback with a self-signed
    certificate for ``storage.test``, trusted by link's TLS contexts. Any
    name resolves (as public) to it, and port 443 is dialled at its port.
    Yields the SNI names it saw."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "storage.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("storage.test")]), False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)
    snis: list[str | None] = []
    server_ctx.sni_callback = lambda sock, sni, ctx: snis.append(sni)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            try:
                conn.settimeout(5)
                tls = server_ctx.wrap_socket(conn, server_side=True)
                tls.recv(4096)
                tls.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n"
                    b"Connection: close\r\n\r\nHELLO"
                )
                tls.close()
            except (OSError, ssl.SSLError):
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    real_context = ssl.create_default_context
    real_dial = socket.create_connection

    def trusting(*args, **kwargs):
        ctx = real_context(*args, **kwargs)
        ctx.load_verify_locations(cert_file)
        return ctx

    def dial(address, *args, **kwargs):
        host, dial_port = address
        return real_dial(
            (host, port if dial_port == 443 else dial_port), *args, **kwargs
        )

    monkeypatch.setattr(link.ssl, "create_default_context", trusting)
    monkeypatch.setattr(link, "_public_address", lambda host, port: "127.0.0.1")
    monkeypatch.setattr(link.socket, "create_connection", dial)
    yield snis
    stop.set()
    thread.join(2)
    listener.close()


class TestPinnedTls:
    def test_the_certificate_is_checked_for_the_url_host(self, tls_server):
        got = bytearray()
        client = RelayClient("https://relay.example.com")
        assert client.get_bytes("https://storage.test/o", got.extend, max_bytes=99) == 5
        assert bytes(got) == b"HELLO"
        assert tls_server == ["storage.test"]  # SNI names the URL host, not the IP
        with pytest.raises(RelayError) as exc:
            client.get_bytes("https://other.test/o", got.extend, max_bytes=99)
        assert exc.value.code == "network"  # the certificate is not other.test's

    def test_a_handshake_that_never_comes_is_cut_at_the_deadline(self, monkeypatch):
        # A host that accepts and then says nothing holds the TLS handshake:
        # the deadline covers it, not just the body.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        real_dial = socket.create_connection
        monkeypatch.setattr(link, "_public_address", lambda host, p: "127.0.0.1")
        monkeypatch.setattr(
            link.socket,
            "create_connection",
            lambda address, *a, **k: real_dial(("127.0.0.1", port), *a, **k),
        )
        started = time.monotonic()
        try:
            with pytest.raises(RelayError) as exc:
                RelayClient("https://relay.example.com", timeout=5).get_bytes(
                    "https://storage.test/o", lambda b: None, max_bytes=10, deadline=0.5
                )
        finally:
            listener.close()
        assert (exc.value.status, exc.value.code) == (0, "timeout")
        assert time.monotonic() - started < 2.5

    def test_a_deadline_already_spent_is_a_timeout_not_a_crash(self, tls_server):
        # Nothing left for the handshake: its timeout is floored, never a
        # negative one (a ValueError that would crash the pull unreported).
        with pytest.raises(RelayError) as exc:
            RelayClient("https://relay.example.com").get_bytes(
                "https://storage.test/o", lambda b: None, max_bytes=99, deadline=0.0
            )
        assert exc.value.code == "timeout"

    def test_a_tls_setup_that_fails_here_refuses_the_url(self, monkeypatch):
        # A broken system certificate store is this machine's fault, not the
        # download's: refused (never retried), and said as such.
        def broken(*_a, **_k):
            raise ssl.SSLError("DECOY broken store")

        monkeypatch.setattr(link.ssl, "create_default_context", broken)
        monkeypatch.setattr(link, "_public_address", lambda host, p: "93.184.216.34")
        with pytest.raises(RelayError) as exc:
            RelayClient("https://relay.example.com").get_bytes(
                "https://storage.test/o", lambda b: None, max_bytes=99
            )
        assert (exc.value.status, exc.value.code) == (0, "invalid_response")
        assert "TLS" in exc.value.message


class TestTlsFloor:
    """Every TLS context the link builds refuses TLS 1.0 and 1.1."""

    def test_the_context_helper_sets_tls_1_2_as_the_floor(self):
        context = link._tls_context()
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname

    def test_the_relay_connection_uses_it(self, monkeypatch):
        # This Python's own default floor may already be 1.2: what is pinned
        # is that the connection takes the helper's context, not a default.
        made = link._tls_context()
        monkeypatch.setattr(link, "_tls_context", lambda: made)
        conn, _target = RelayClient("https://relay.example.com")._connection(
            "https://relay.example.com/v1/host", 5.0
        )
        assert conn._context is made

    def test_the_pinned_download_connection_uses_it(self, monkeypatch):
        made = link._tls_context()
        monkeypatch.setattr(link, "_tls_context", lambda: made)
        monkeypatch.setattr(link, "_public_address", lambda host, port: "192.0.2.10")
        conn, _target = RelayClient("https://relay.example.com")._public_connection(
            "https://storage.example.com/o?sig=DECOY_s", lambda: 5.0
        )
        assert conn._context is made


class TestDeadline:
    def test_a_socket_handed_over_after_the_deadline_is_cut_at_once(self):
        ours, theirs = socket.socketpair()
        theirs.settimeout(2)
        try:
            with link._Deadline(None, 0.0) as cut:
                ends = time.monotonic() + 2
                while not cut.expired and time.monotonic() < ends:
                    time.sleep(0.01)
                assert cut.expired
                cut.watch(ours)
                assert theirs.recv(1) == b""  # cut: the peer reads the end
        finally:
            ours.close()
            theirs.close()


class TestStreamBounds:
    def test_a_line_past_the_cap_is_refused(self, server):
        with pytest.raises(RelayError) as exc:
            list(RelayClient(server).stream("/sse-long", read_timeout=10))
        assert (exc.value.code, exc.value.message) == (
            "invalid_response",
            "SSE line too long",
        )

    def test_a_line_at_the_cap_is_read(self, server):
        events = list(RelayClient(server).stream("/sse-edge", read_timeout=10))
        assert len(events) == 1
        assert len(events[0].data) == link.MAX_SSE_LINE - len("data: \n")

    def test_a_reply_that_is_not_an_event_stream_is_refused(self, server):
        with pytest.raises(RelayError) as exc:
            list(RelayClient(server).stream("/sse-json"))
        assert (exc.value.status, exc.value.code) == (200, "invalid_response")

    def test_an_event_past_the_size_cap_is_refused(self):
        lines = [b"data: aaaa\n", b"data: bbbb\n", b"\n"]
        assert list(iter_sse(lines, max_event_bytes=10)) == [
            SseEvent("message", "aaaa\nbbbb", None)
        ]
        with pytest.raises(RelayError) as exc:
            list(iter_sse([*lines[:2], b"data: c\n", b"\n"], max_event_bytes=10))
        assert (exc.value.code, exc.value.message) == (
            "invalid_response",
            "SSE event too large",
        )

    def test_the_size_cap_counts_one_event_at_a_time(self):
        lines = [b"data: aaaa\n", b"\n"] * 5
        assert len(list(iter_sse(lines, max_event_bytes=10))) == 5


class TestReadBounds:
    def test_readline_asks_for_at_most_the_line_cap(self):
        class _Resp:
            def __init__(self) -> None:
                self.limits: list[int] = []

            def readline(self, limit: int = -1) -> bytes:
                self.limits.append(limit)
                return b"data: x\n"

        resp = _Resp()
        assert link._readline(resp) == b"data: x\n"
        assert resp.limits == [link.MAX_SSE_LINE + 1]

    def test_stream_applies_the_event_cap(self, server, monkeypatch):
        monkeypatch.setattr(link, "MAX_JSON_BYTES", 1000)
        with pytest.raises(RelayError) as exc:
            list(RelayClient(server).stream("/sse-big", read_timeout=10))
        assert (exc.value.code, exc.value.message) == (
            "invalid_response",
            "SSE event too large",
        )

    def test_a_reply_past_the_body_cap_is_refused(self, server, monkeypatch):
        monkeypatch.setattr(link, "MAX_JSON_BYTES", 1000)
        with pytest.raises(RelayError) as exc:
            RelayClient(server).call("GET", "/big")
        assert (exc.value.status, exc.value.code, exc.value.message) == (
            200,
            "invalid_response",
            "reply too large",
        )

    def test_the_body_read_stops_at_the_cap(self, server, monkeypatch):
        # An unbounded read would sit out the server's silence and then lose
        # the connection (stream_lost); the bounded one never waits for it.
        monkeypatch.setattr(link, "MAX_JSON_BYTES", 1000)
        with pytest.raises(RelayError) as exc:
            RelayClient(server, timeout=5).call("GET", "/big-hang")
        assert (exc.value.code, exc.value.message) == (
            "invalid_response",
            "reply too large",
        )

    @pytest.mark.parametrize("path", ["/drip", "/drip-open"])
    def test_a_dripping_reply_is_cut_at_the_call_deadline(self, server, path):
        # The timeout bounds the whole call, not each read: a relay that
        # drips one byte at a time cannot hold the caller past it.
        started = time.monotonic()
        with pytest.raises(RelayError) as exc:
            RelayClient(server, timeout=0.5).call("GET", path)
        assert (exc.value.status, exc.value.code) == (0, "timeout")
        assert time.monotonic() - started < 2.5


class TestIterSse:
    def test_multi_line_data_and_crlf(self):
        lines = [b"event: x\r\n", b"data: a\r\n", b"data: b\r\n", b"\r\n"]
        assert list(iter_sse(lines)) == [SseEvent("x", "a\nb", None)]

    def test_no_event_name_is_message_and_no_data_is_not_dispatched(self):
        lines = [b"data:plain\n", b"\n", b"event: empty\n", b"\n"]
        assert list(iter_sse(lines)) == [SseEvent("message", "plain", None)]

    def test_an_unterminated_block_is_dropped(self):
        assert list(iter_sse([b"data: half\n"])) == []


class TestBackoff:
    def test_full_jitter_doubles_to_the_cap_and_resets(self):
        b = Backoff(rand=lambda: 1.0)
        assert [b.next_delay() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
        b.reset()
        assert b.next_delay() == 1

    def test_the_delay_is_scaled_by_the_random_draw(self):
        assert Backoff(rand=lambda: 0.25).next_delay() == 0.25


class TestSeenRing:
    def test_add_reports_repeats_and_evicts_the_oldest(self):
        ring = SeenRing(size=2)
        assert ring.add("a") and ring.add("b")
        assert not ring.add("a")
        assert ring.add("c")
        assert ring.items() == ("b", "c")
        assert ring.add("a")

    def test_it_restores_from_items(self):
        ring = SeenRing(["x", "y"], size=5)
        assert "x" in ring and not ring.add("y")


class TestEnvReaders:
    def test_on_by_default_and_off_at_zero(self, monkeypatch):
        monkeypatch.setenv("MAGENT_LINK", "1")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.link_enabled() is True
        monkeypatch.setenv("MAGENT_LINK", "0")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.link_enabled() is False

    def test_relay_url_has_no_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("MAGENT_LINK_URL", "https://relay.example.com/")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.relay_url() == "https://relay.example.com"

    def test_a_broken_env_fails_open_with_defaults(self, monkeypatch):
        monkeypatch.setenv("MAGENT_NOT_A_THING", "1")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.link_enabled() is True
        assert link.relay_url() == "https://api.magent.now"
