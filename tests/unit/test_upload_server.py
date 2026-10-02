import io
import json
import logging
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import ClassVar

import pytest

from magent.psmux import config_sessions
from magent.upload_server import (
    UploadHandler,
    _build_html,
    _parse_multipart,
)


class TestBuildHtml:
    def test_renders_sessions(self):
        sessions = [
            {"name": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "path": "INTERNAL/upup"},
        ]
        html = _build_html(sessions)
        assert "marka" in html
        assert "upup" in html
        assert "pill" in html

    def test_no_sessions_shows_message(self):
        html = _build_html([])
        assert "no active sessions" in html

    def test_pill_wire_value_is_session_id(self):
        # P3-01: the pill's data-name (the value posted back as `project`) is
        # the psmux socket id, not the display name.
        html = _build_html([{"name": "my.api", "session": "my-api", "path": "x"}])
        assert 'data-name="my-api"' in html

    def test_clipboard_paste_ui_ships_on_the_page(self):
        # Ctrl+V flow contract: the staged-image confirm panel (preview img,
        # destination-project line, progress bar, explicit Send/Cancel) and the
        # window paste listener must all be present in the served page. The
        # real-browser behavioural proof lives in the `browser` e2e tier.
        html = _build_html([{"name": "p", "path": "x"}])
        for anchor in (
            'id="paste-box"',
            'id="paste-img"',
            'id="paste-dest"',
            'id="paste-bar"',
            'id="paste-send"',
            'id="paste-cancel"',
            "addEventListener('paste'",
            "XMLHttpRequest",
        ):
            assert anchor in html, f"paste-upload UI anchor missing: {anchor}"


class TestTheProjectPickerFiltersAsYouType:
    """The mobile picker is type-to-filter, and tap stays the primary gesture.

    A fleet outgrows a thumb's scroll long before it outgrows the config, so
    the page carries a text box that live-filters the pills, highlights the
    best match, and takes Enter/ArrowUp/ArrowDown -- while every pill stays a
    tap target and nothing below the input needs a keyboard.

    These are drift pins on the SERVED page, in the same cheap style as the
    paste-state pins above: they fail the moment an element, a key, or a
    ranking tier is dropped. The behavioural proof -- typing into real
    Chromium, reading the filtered list, selecting by tap AND by Enter -- is
    the `browser` e2e tier.
    """

    def _html(self, *names: str) -> str:
        return _build_html([{"name": n, "path": "x"} for n in (names or ("p",))])

    def test_the_filter_input_and_its_chrome_ship_on_the_page(self):
        html = self._html("alpha", "beta")
        for anchor in (
            'id="proj-filter"',  # the text box itself
            'id="pills"',  # ...the list it filters
            'id="proj-nomatch"',  # ...what a query matching nothing says
            'id="proj-chosen"',  # ...and the standing "which project" answer
        ):
            assert anchor in html, f"typeahead anchor missing: {anchor}"
        # Touch-first: the box is an aid, never a gate. Autofocus would pop the
        # phone keyboard over the pills the user came to tap.
        assert "autofocus" not in html, "the filter must not steal focus on a phone"

    def test_the_keys_are_wired_on_the_filter_box(self):
        html = self._html("alpha", "beta")
        for anchor in (
            "filterBox.addEventListener('input', renderFilter)",
            "filterBox.addEventListener('keydown'",
            "'ArrowDown'",
            "'ArrowUp'",
            "e.key === 'Enter'",
        ):
            assert anchor in html, f"typeahead key wiring missing: {anchor}"

    def test_enter_selects_through_the_same_click_a_thumb_would(self):
        # One selection path, not two: Enter dispatches the highlighted pill's
        # own click, so the keyboard can never diverge from the tap -- which is
        # also what keeps `refreshPaste` (the Send gate) firing either way.
        assert "shown[hi].click()" in self._html("alpha", "beta")

    def test_the_highlight_is_a_class_distinct_from_the_selection(self):
        # `hi` (where Enter would land) and `on` (what is selected) answer
        # different questions and must both be readable at once.
        html = self._html("alpha", "beta")
        assert ".pill.hi{" in html, "the highlight lost its own style"
        assert "classList.add('hi')" in html
        assert "classList.add('on')" in html

    def test_ranking_mirrors_the_cli_tiers_in_order(self):
        # Case-insensitive, and scored by HOW the name matched: prefix beats
        # word-boundary beats substring beats in-order subsequence, with ties
        # left in config order by a stable sort. Drift here means the same
        # query picks different projects on the phone and in the menu.
        html = self._html("alpha", "beta")
        assert (
            "const T_PREFIX = 0, T_WORD = 1, T_SUB = 2, T_SUBSEQ = 3, T_NONE = 4;"
            in html
        ), "the ranking tiers changed shape or order"
        assert "function matchTier(" in html
        assert "toLowerCase()" in html, "ranking stopped being case-insensitive"
        assert "n.startsWith(q)" in html  # prefix
        assert "n.startsWith(q, i)" in html  # word boundary
        assert "n.includes(q)" in html  # substring
        assert "a.t - b.t || a.i - b.i" in html, (
            "ties stopped falling back to config order"
        )

    def test_a_filtered_out_pill_is_hidden_not_destroyed(self):
        # Hiding keeps the pill's listeners and its config index, so clearing
        # the query restores exactly the list the config wrote.
        html = self._html("alpha", "beta")
        assert ".pill.off{display:none}" in html
        assert "classList.add('off')" in html
        assert "classList.remove('off')" in html

    def test_the_selected_project_survives_being_filtered_out(self):
        # The query can hide the pill carrying `.on`; the standing line must
        # still say which session the next Send goes to.
        html = self._html("alpha", "beta")
        assert "chosen.textContent = 'project: ' + proj" in html
        assert ">no project selected<" in html

    def test_send_is_still_gated_on_a_selected_project(self):
        # The typeahead adds a way to CHOOSE, never a way to skip choosing.
        html = self._html("alpha", "beta")
        assert 'id="paste-send" disabled' in html, "Send no longer starts disabled"
        assert "psend.disabled = sending || !proj" in html, (
            "Send stopped being gated on a selected project"
        )
        assert '<input type="file" id="file" multiple disabled>' in html

    def test_no_sessions_means_no_filter_box_to_type_into(self):
        # An input whose only possible answer is "no match" is noise; the
        # empty-list message already says everything there is to say.
        html = _build_html([])
        assert "no active sessions" in html
        assert "filterBox.style.display = 'none'" in html


class TestThePageReadsAllThreePasteStates:
    """The mobile page must not collapse `inject_pending` into a failure.

    `/upload` answers with three paste states -- pasted, still trying, refused
    (DESIGN.md "The upload reply is not hostage to the paste"). The page's JS
    read only `injected`, so the slow-but-successful paste -- the exact
    condition the server-side fix exists for -- rendered on the phone as a
    failure-looking result about a file that was already safely on disk.

    These are drift pins on the SERVED page, deliberately cheap: they fail the
    moment the field or the shared wording is dropped again. The behavioural
    proof (a real browser, a real serve, a real multiplexer stalling past
    INJECT_GRACE_S) is the `browser` e2e tier.
    """

    def _html(self) -> str:
        return _build_html([{"name": "p", "path": "x"}])

    def test_the_page_js_reads_the_inject_pending_field(self):
        html = self._html()
        assert "inject_pending" in html, (
            "the page dropped `inject_pending`; a slow paste is a failure again"
        )
        assert "d.injected" in html  # ...and still distinguishes the fast one

    def test_the_pending_wording_matches_the_alt_v_narration(self):
        # One vocabulary for one event: the phone and the psmux status line
        # must not describe the same pending paste differently.
        from magent.altv import OUTCOME_REASONS

        shared = OUTCOME_REASONS["inject-pending"].removeprefix("image ")
        assert shared in self._html(), (
            f"the page's pending wording drifted from altv's {shared!r}"
        )

    def test_pending_is_tinted_healthy_and_never_as_an_error(self):
        # Same call the status line makes: the bytes are on disk, so red would
        # read as "your screenshot is gone". Both result surfaces (the drop
        # zone and the toast) carry the ok class alongside the pend marker.
        html = self._html()
        for healthy in ("'drop ok pend'", "'toast ok pend'"):
            assert healthy in html, f"pending lost its healthy tint: {healthy}"
        for wrong in ("drop err pend", "toast err pend"):
            assert wrong not in html, f"pending is styled as a failure: {wrong}"


class TestThePageTakesAnyFile:
    """The phone page uploads ANY file, not just images -- on both of its
    paths (the file picker and Ctrl+V), under one size limit it checks BEFORE
    sending.

    Drift pins on the served page, same cheap style as the classes above; the
    real-browser proof of a non-image paste is the `browser` e2e tier.
    """

    def _html(self) -> str:
        return _build_html([{"name": "p", "path": "x"}])

    def test_the_picker_has_no_type_restriction_and_takes_several(self):
        html = self._html()
        assert '<input type="file" id="file" multiple disabled>' in html
        assert "accept=" not in html, "the file picker is filtering types again"

    def test_a_pasted_file_of_any_type_is_staged(self):
        html = self._html()
        assert "it.kind !== 'file'" in html
        assert "it.type.startsWith('image/')" not in html, (
            "the paste handler still takes images only"
        )

    def test_every_pasted_file_is_staged_not_just_the_first(self):
        # Copying three files and pressing Ctrl+V used to stage ONE and drop
        # the rest in silence. Every file item is collected, then staged as
        # one selection.
        html = self._html()
        assert "files.push(f)" in html
        assert "stageFiles(files)" in html
        assert "stageFile(f);\n      return;" not in html, (
            "the paste handler stops at the first file again"
        )

    def test_several_files_go_as_one_request_with_one_part_each(self):
        # One request, one paste: the same shape an Alt+V press sends.
        html = self._html()
        assert "for (const f of files) form.append('file', f);" in html
        assert "for (const s of staged.files) form.append('file', s.file, s.name);" in (
            html
        )
        # ...and the reply's own count of saved files is what the page reports.
        assert html.count("sentLabel(d, ") >= 2
        assert "(d.paths || []).length" in html

    def test_a_folder_is_refused_in_the_same_words_as_alt_v(self):
        from magent.altv import OUTCOME_REASONS

        html = self._html()
        assert f"const FOLDER = '{OUTCOME_REASONS['folder-refused']}';" in html
        # The entry is the authority where the browser exposes one; the empty
        # typeless File is the fallback shape of a folder.
        assert "isDirectory" in html
        assert "f.size === 0 && !f.type" in html
        # Both surfaces refuse: the paste stager and the picker.
        assert html.count("FOLDER)") >= 2

    def test_the_entry_decides_before_the_size_and_type_guess(self):
        # The guess (empty + no MIME type) is only a stand-in for an entry the
        # browser did not give. Where it DID give one, the guess must never
        # overrule it: an empty .toml dropped in Chrome reports isDirectory
        # false and used to be refused as a folder anyway.
        html = self._html()
        body = html.split("function itemIsFolder(it) {", 1)[1].split("\n}", 1)[0]
        assert "if (entry) return entry.isDirectory;" in body
        assert body.index("entry.isDirectory") < body.index("looksLikeFolder(")
        # Paste and drop both decide through that one function.
        assert html.count("itemIsFolder(it)") >= 3

    def test_a_plain_pick_is_never_guessed_to_be_a_folder(self):
        # A picker cannot select a folder, so on a plain pick the guess only
        # ever refuses real empty files. Only a DROP can bring a folder, and
        # the drop listener decides that from the drop's own items.
        html = self._html()
        change = html.split("input.addEventListener('change'", 1)[1]
        change = change.split("\n});", 1)[0]
        assert "looksLikeFolder" not in change
        assert "const folder = droppedFolder;" in change
        drop = html.split("input.addEventListener('drop'", 1)[1].split("\n});", 1)[0]
        assert "itemIsFolder(it)" in drop
        # Opening the picker forgets a drop that never became a selection, so
        # it cannot refuse the next plain pick.
        assert "input.addEventListener('click', () => { droppedFolder = false; });" in (
            html
        )

    def test_a_non_image_shows_a_file_tile_not_a_broken_preview(self):
        html = self._html()
        for anchor in ('id="paste-file"', 'id="paste-name"', "pfile.className"):
            assert anchor in html, f"non-image paste tile missing: {anchor}"
        # Images keep their preview: the <img> is toggled, not deleted.
        assert 'id="paste-img"' in html

    def test_the_staged_name_keeps_the_original_filename(self):
        # A pasted file keeps its own name; only a nameless blob falls back
        # to the generated paste-<ts>.<ext>.
        html = self._html()
        assert "f.name || ('paste-' + ts + '.' + extFor(f.type))" in html

    def test_the_page_refuses_an_over_limit_send_before_sending_it(self):
        from magent.sessions import MAX_UPLOAD_BYTES, upload_limit_text

        html = self._html()
        assert f"const MAX_BYTES = {MAX_UPLOAD_BYTES};" in html
        assert f"const MAX_LABEL = '{upload_limit_text(MAX_UPLOAD_BYTES)}';" in html
        assert "PLACEHOLDER" not in html, "a template placeholder leaked"
        # The limit is per send, so both paths check the SUM of the selection:
        # the picker's change handler and the paste stager.
        assert html.count("files.reduce((n, f) => n + f.size, 0)") >= 2
        assert html.count("total > MAX_BYTES") >= 2
        assert "'too large - ' + MAX_LABEL + ' limit'" in html

    def test_the_over_limit_words_are_alt_vs_words(self):
        # Same binding as the folder and pending wording: the page's refusal,
        # rendered with the label it is served, is Alt+V's too-large reason.
        from magent.altv import OUTCOME_REASONS

        html = self._html()
        assert "const TOO_BIG = 'too large - ' + MAX_LABEL + ' limit';" in html
        label = html.split("const MAX_LABEL = '", 1)[1].split("';", 1)[0]
        assert f"too large - {label} limit" == OUTCOME_REASONS["too-large"]

    def test_a_picker_refusal_writes_its_own_toast_text(self):
        # Turning the toast red without replacing its text would re-colour
        # whatever stale message it held last as this failure.
        html = self._html()
        assert "function pickFail(msg)" in html
        body = html.split("function pickFail(msg)", 1)[1].split("}", 1)[0]
        assert "toast.textContent = msg;" in body
        assert "toast.className = 'toast err';" in body


class TestConfigSessions:
    def test_carries_display_name_and_sanitized_session(self, tmp_path):
        # P3-01: _config_sessions splits the display name from the psmux id.
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "path": str(tmp_path / "svc"),
                            "title": "my.api",
                            "tool": "claude",
                        }
                    ]
                }
            )
        )
        out = config_sessions(str(cfg))
        assert out[0]["name"] == "my.api"
        assert out[0]["session"] == "my-api"

    def _cfg(self, tmp_path, projects, base_dir=None):
        cfg = tmp_path / "magent.config.json"
        body: dict[str, object] = {"projects": projects}
        if base_dir is not None:
            body["baseDir"] = base_dir
        cfg.write_text(json.dumps(body))
        return str(cfg)

    def test_absolute_path_stays_itself(self, tmp_path):
        # `resolved` is what a client can actually act on (the F2 open-in-code
        # hotkey); `path` is the raw config value and may be relative.
        (tmp_path / "svc").mkdir()
        out = config_sessions(
            self._cfg(tmp_path, [{"path": str(tmp_path / "svc"), "tool": "claude"}])
        )
        assert out[0]["resolved"] == str(tmp_path / "svc")

    def test_relative_path_resolves_against_base_dir(self, tmp_path):
        (tmp_path / "INTERNAL" / "caly").mkdir(parents=True)
        out = config_sessions(
            self._cfg(
                tmp_path,
                [{"path": "INTERNAL/caly", "tool": "claude"}],
                base_dir=str(tmp_path),
            )
        )
        assert out[0]["path"] == "INTERNAL/caly"  # raw value untouched
        assert Path(out[0]["resolved"]) == tmp_path / "INTERNAL" / "caly"

    def test_unresolvable_path_is_empty_string(self, tmp_path):
        # Never None: a JSON consumer reads it as a plain string field.
        out = config_sessions(
            self._cfg(tmp_path, [{"path": "nope/missing", "tool": "claude"}])
        )
        assert out[0]["resolved"] == ""

    def test_html_escapes_names(self):
        sessions = [{"name": "<b>bad</b>", "path": "x&y"}]
        html = _build_html(sessions)
        assert "<b>bad</b>" not in html
        assert "&lt;b&gt;bad&lt;/b&gt;" in html


class TestParseMultipart:
    def _make_handler(self, body: bytes, boundary: str):
        class FakeHandler:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            rfile = io.BytesIO(body)

        return FakeHandler()

    def test_parses_field_and_file(self):
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"PNGDATA\r\n"
            b"------TestBoundary--\r\n"
        )

        handler = self._make_handler(body, "----TestBoundary")
        fields, files = _parse_multipart(handler)

        assert fields["project"] == "marka"
        # Every same-named file part, in order -- one Alt+V press can carry a
        # whole Explorer selection in one request.
        assert files["file"] == [("test.png", b"PNGDATA")]

    def test_several_file_parts_under_one_name_all_survive_in_order(self):
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.zip"\r\n'
            b"\r\n"
            b"ZIP\r\n"
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="b.py"\r\n'
            b"\r\n"
            b"print(1)\r\n"
            b"------TestBoundary--\r\n"
        )
        _fields, files = _parse_multipart(self._make_handler(body, "----TestBoundary"))
        assert files["file"] == [("a.zip", b"ZIP"), ("b.py", b"print(1)")]

    def test_a_semicolon_in_a_filename_does_not_cut_it_short(self):
        # Any file uploads now, and `;` is legal in a filename on every OS; the
        # header's own `;` token split must not truncate the name at it.
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a;b c.txt"\r\n'
            b"\r\n"
            b"X\r\n"
            b"------TestBoundary--\r\n"
        )
        _fields, files = _parse_multipart(self._make_handler(body, "----TestBoundary"))
        assert files["file"] == [("a;b c.txt", b"X")]

    def test_parse_multipart_missing_boundary_returns_empty(self):
        # F-D3-006: Content-Type with no boundary= is treated as "no body".
        class FakeHandler:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "multipart/form-data",
                "Content-Length": "0",
            }
            rfile = io.BytesIO(b"")

        assert _parse_multipart(FakeHandler()) == ({}, {})

    def test_bad_content_length_no_crash(self):
        # F-D3-002: a non-numeric Content-Length used to raise an uncaught
        # ValueError from int() inside the parser; it's now treated as "no
        # body" instead of crashing the request-handling thread.
        class FakeHandler:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "multipart/form-data; boundary=X",
                "Content-Length": "abc",
            }
            rfile = io.BytesIO(b"")

        assert _parse_multipart(FakeHandler()) == ({}, {})

    _WHOLE = (
        b"------TestBoundary\r\n"
        b'Content-Disposition: form-data; name="project"\r\n'
        b"\r\n"
        b"marka\r\n"
        b"------TestBoundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="v.mp4"\r\n'
        b"\r\n"
        b"FRAMES\r\n"
        b"------TestBoundary--\r\n"
    )

    def test_a_body_shorter_than_it_declared_is_incomplete(self):
        # The client died mid-upload: what arrived is not the file, and must
        # never be saved and pasted as if it were.
        from magent.upload_server import UploadIncomplete

        handler = self._make_handler(self._WHOLE, "----TestBoundary")
        handler.rfile = io.BytesIO(self._WHOLE[:-30])
        with pytest.raises(UploadIncomplete):
            _parse_multipart(handler)

    def test_a_short_body_is_incomplete_even_when_its_closing_delimiter_arrived(
        self,
    ):
        # The ONE cut the delimiter check cannot see: every part closed, only
        # the final CRLF missing. Only the declared-length check refuses it, so
        # this is the case that pins it (a 30-byte cut also loses the delimiter).
        from magent.upload_server import UploadIncomplete

        handler = self._make_handler(self._WHOLE, "----TestBoundary")
        handler.rfile = io.BytesIO(self._WHOLE[:-2])
        with pytest.raises(UploadIncomplete):
            _parse_multipart(handler)

    def test_a_body_without_its_closing_delimiter_is_incomplete(self):
        # Declared length and delivered length agree, but the last part is
        # never closed -- a sender that stopped mid-part. Its data cannot be
        # trusted to be whole.
        from magent.upload_server import UploadIncomplete

        body = self._WHOLE[: -len(b"------TestBoundary--\r\n")]
        with pytest.raises(UploadIncomplete):
            _parse_multipart(self._make_handler(body, "----TestBoundary"))

    def test_a_line_that_only_starts_like_the_delimiter_is_data(self):
        # A delimiter is the boundary followed by CRLF or `--`; anything else
        # after it is the file's own bytes.
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.log"\r\n'
            b"\r\n"
            b"one\r\n------TestBoundaryX two\r\n"
            b"------TestBoundary--\r\n"
        )
        _fields, files = _parse_multipart(self._make_handler(body, "----TestBoundary"))
        assert files["file"] == [("a.log", b"one\r\n------TestBoundaryX two")]

    def test_parsing_never_copies_the_body(self):
        # One upload can be 100 MB and serve runs on a memory-starved box: the
        # parts are views into the one body read off the socket, never copies
        # of it (the split-based parser held about four bodies at its peak).
        import tracemalloc

        data = bytes(range(256)) * (32 * 1024)  # 8 MiB
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
            b"\r\n" + data + b"\r\n------TestBoundary--\r\n"
        )
        handler = self._make_handler(body, "----TestBoundary")
        tracemalloc.start()
        try:
            _fields, files = _parse_multipart(handler)
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # The one read of the body itself, and nothing on top of it.
        assert peak < len(body) * 1.25, f"peak {peak} for a {len(body)}-byte body"
        ((_name, part),) = files["file"]
        assert isinstance(part, memoryview)
        assert part == data


class _DrainConn:
    """Socket stand-in exposing only the timeout knobs the drain touches."""

    def __init__(self):
        self._timeout = None

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value


class _DrainReader:
    """rfile stand-in: yields buffered bytes, then (opt-in) signals "nothing
    more pending" the way a timed-out blocking socket read does -- by raising."""

    def __init__(self, data: bytes, *, raise_when_empty: bool = False):
        self._buf = io.BytesIO(data)
        self.consumed = 0
        self._raise_when_empty = raise_when_empty

    def read(self, n: int) -> bytes:
        chunk = self._buf.read(n)
        if not chunk and self._raise_when_empty:
            raise TimeoutError  # socket.timeout is an OSError subclass
        self.consumed += len(chunk)
        return chunk


class _DrainHandler:
    """Carries only the attributes _drain_request_body reads and writes."""

    def __init__(self, reader: _DrainReader, content_length: str | None):
        self.close_connection = False
        self.rfile = reader
        self.connection = _DrainConn()
        self.headers = (
            {} if content_length is None else {"Content-Length": content_length}
        )


class TestDrainRequestBody:
    """P4-02: the bounded body-drain that lets an early 4xx land cleanly rather
    than as a Windows TCP RST. It must never read past the cap, and must tolerate
    an absent/garbage Content-Length without blocking or propagating."""

    def _drain(self, reader: _DrainReader, content_length: str | None) -> _DrainHandler:
        handler = _DrainHandler(reader, content_length)
        # Call unbound: _drain_request_body only touches the stubbed attributes.
        UploadHandler._drain_request_body(handler)
        return handler

    def test_reads_at_most_the_cap(self, monkeypatch):
        # Feed a source far larger than the cap: consumption must stop at the cap.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"x" * 500)
        handler = self._drain(reader, str(500))

        assert reader.consumed == 100  # bounded -- never the full 500
        assert handler.close_connection is True

    def test_tolerates_garbage_content_length(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"y" * 20)
        handler = self._drain(reader, "not-a-number")

        assert reader.consumed == 20  # drained all available, then hit EOF
        assert handler.close_connection is True

    def test_tolerates_absent_content_length(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"z" * 15)
        handler = self._drain(reader, None)  # no Content-Length header at all

        assert reader.consumed == 15
        assert handler.close_connection is True

    def test_stops_on_read_timeout(self, monkeypatch):
        # A blocking socket that times out mid-drain (client declared more than
        # it sent and holds the connection open) raises OSError -- swallowed.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"w" * 5, raise_when_empty=True)
        handler = self._drain(reader, "garbage")

        assert reader.consumed == 5
        assert handler.close_connection is True

    def test_zero_length_is_noop(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"unused", raise_when_empty=True)
        handler = self._drain(reader, "0")

        assert reader.consumed == 0  # nothing declared -> nothing read
        assert handler.close_connection is True
        assert handler.connection.gettimeout() is None  # socket never touched


class TestUploadServerIntegration:
    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: None)
        self.upload_dir = tmp_path / "uploads"

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [
            {"name": "marka", "session": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "session": "upup", "path": "INTERNAL/upup"},
        ]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        yield
        self.server.shutdown()

    def _conn(self):
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_get_index(self):
        conn = self._conn()
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read().decode()
        assert "marka" in body
        assert "upup" in body

    def test_get_api_sessions(self):
        conn = self._conn()
        conn.request("GET", "/api/sessions")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        # P3-04/P3-18: ok-envelope + the LIST lives under `sessions`.
        assert data["ok"] is True
        assert len(data["sessions"]) == 2
        # P3-01: each entry carries the display `name` and psmux `session`.
        assert data["sessions"][0]["name"] == "marka"
        assert data["sessions"][0]["session"] == "marka"

    def test_get_on_post_only_path_is_405(self):
        # P3-16: wrong method on a real route -> 405 (not 404).
        conn = self._conn()
        conn.request("GET", "/upload")
        resp = conn.getresponse()
        assert resp.status == 405
        data = json.loads(resp.read())
        assert data["ok"] is False
        assert data["error"]

    def test_post_on_get_only_path_is_405(self):
        conn = self._conn()
        conn.request("POST", "/", body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        assert resp.status == 405
        assert json.loads(resp.read())["ok"] is False

    def test_unknown_path_is_404_json_envelope(self):
        # P3-16: a genuinely unknown path stays 404, as the JSON error envelope.
        conn = self._conn()
        conn.request("GET", "/does-not-exist")
        resp = conn.getresponse()
        assert resp.status == 404
        data = json.loads(resp.read())
        assert data["ok"] is False
        assert data["error"]

    def test_post_unknown_path_is_404(self):
        conn = self._conn()
        conn.request("POST", "/nope", body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        assert resp.status == 404
        assert json.loads(resp.read())["ok"] is False

    def test_upload_saves_file(self):
        body = (
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n'
            b"\r\n"
            b"0\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="screenshot.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"FAKEPNG\r\n"
            b"------WebKitFormBoundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----WebKitFormBoundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())

        assert data["ok"] is True
        assert "screenshot.png" in data["path"]
        assert not data["injected"]
        saved = Path(data["path"])
        assert saved.exists()
        assert saved.read_bytes() == b"FAKEPNG"

    def _post_files(self, *files: tuple[str, bytes]) -> dict:
        parts = [
            (
                b"------B\r\n"
                b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
                b"------B\r\n"
                b'Content-Disposition: form-data; name="inject"\r\n\r\n0\r\n'
            )
        ]
        for name, data in files:
            parts.append(
                b"------B\r\n"
                + f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode()
                + b"Content-Type: application/octet-stream\r\n\r\n"
                + data
                + b"\r\n"
            )
        body = b"".join(parts) + b"------B--\r\n"
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        return json.loads(conn.getresponse().read())

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("bundle.zip", b"PK\x03\x04" + bytes(range(256)) * 4),
            ("script.py", b"print('hi')\r\nprint(2)\n"),
            ("Makefile", b"all:\n\techo ok\n"),
        ],
    )
    def test_any_file_type_lands_byte_identical(self, name, data):
        reply = self._post_files((name, data))
        assert reply["ok"] is True
        saved = Path(reply["path"])
        assert saved.name.endswith(f"_{name}")
        assert saved.read_bytes() == data

    def test_several_files_in_one_request_all_land(self):
        reply = self._post_files(("a.zip", b"ZIP\x00\x01"), ("b.py", b"print(1)"))
        assert reply["ok"] is True
        saved = [Path(p) for p in reply["paths"]]
        assert [p.read_bytes() for p in saved] == [b"ZIP\x00\x01", b"print(1)"]
        assert saved[0].name.endswith("_a.zip") and saved[1].name.endswith("_b.py")
        assert reply["path"] == reply["paths"][0]  # the one-file field is kept

    def test_a_name_with_control_characters_is_saved_and_pasted_clean(
        self, monkeypatch
    ):
        # The saved name is what the server pastes, so it must never carry a
        # control or line-break character -- whatever the sender called it.
        sent = self._pastes(monkeypatch)
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="'
            + "a\u2028b\u0085c\x1bd\u2029e.txt".encode()
            + b'"\r\nContent-Type: text/plain\r\n\r\nhi\r\n------B--\r\n'
        )
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        reply = json.loads(conn.getresponse().read())
        assert reply["ok"] is True
        saved = Path(reply["path"])
        assert saved.name.endswith("_a_b_c_d_e.txt")
        assert saved.read_bytes() == b"hi"
        assert len(sent) == 1
        pasted = sent[0][0][1]
        assert pasted == str(saved)
        assert not any(ch in pasted for ch in "\u2028\u0085\x1b\u2029\r\n")

    def test_a_failed_write_takes_the_whole_request_back(self, monkeypatch):
        # Reserved names are real (empty) files; a request that fails part-way
        # must not leave them -- or the files it did write -- behind.
        real = Path.write_bytes
        calls = {"n": 0}

        def _second_fails(self, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            return real(self, data)

        monkeypatch.setattr(Path, "write_bytes", _second_fails)
        reply = self._post_files(("a.txt", b"first"), ("b.txt", b"second"))
        assert reply == {"ok": False, "error": "internal"}
        assert self._saved() == []

    def test_two_files_with_one_name_never_overwrite_each_other(self):
        reply = self._post_files(("notes.txt", b"first"), ("notes.txt", b"second"))
        saved = [Path(p) for p in reply["paths"]]
        assert len(set(saved)) == 2
        assert [p.read_bytes() for p in saved] == [b"first", b"second"]

    def _pastes(self, monkeypatch) -> list[tuple]:
        import magent.psmux as psmux_mod

        sent: list[tuple] = []
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux_mod, "send_keys", lambda *a, **k: sent.append((a, k)) or True
        )
        return sent

    def _saved(self) -> list[Path]:
        return list(self.upload_dir.glob("*")) if self.upload_dir.exists() else []

    _CUT = (
        b"------B\r\n"
        b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
        b"------B\r\n"
        b'Content-Disposition: form-data; name="inject"\r\n\r\n1\r\n'
        b"------B\r\n"
        b'Content-Disposition: form-data; name="file"; filename="v.mp4"\r\n\r\n'
        + b"A" * 4096
        + b"\r\n------B--\r\n"
    )

    def test_a_body_cut_short_is_refused_and_nothing_is_saved(self, monkeypatch):
        # The client dies mid-upload (a phone off wifi, a listener giving up on
        # a slow link). What arrived must not become a file that is announced
        # "uploaded" and whose path is pasted into the agent.
        import socket

        sent = self._pastes(monkeypatch)
        body = self._CUT
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(
                b"POST /upload HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: multipart/form-data; boundary=----B\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body[: len(body) - 2000]
            )
            s.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        head, _, payload = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.0 400"), head
        assert json.loads(payload) == {"ok": False, "error": "Upload incomplete"}
        assert self._saved() == []
        assert sent == []

    def test_a_client_that_stalls_mid_body_is_let_go(self, monkeypatch):
        # A client that declares a body and then goes silent used to pin its
        # handler thread (and the partial buffer) forever. The per-operation
        # socket timeout ends it: "Upload incomplete", nothing saved, and the
        # server is free for the next request.
        import socket

        monkeypatch.setattr(UploadHandler, "timeout", 0.5)
        sent = self._pastes(monkeypatch)
        body = self._CUT
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(
                b"POST /upload HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: multipart/form-data; boundary=----B\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body[: len(body) // 2]
            )
            # No shutdown: the client is still there, just saying nothing.
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        assert time.monotonic() - started < 8, "the stalled client was never let go"
        head, _, payload = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.0 400"), head
        assert json.loads(payload) == {"ok": False, "error": "Upload incomplete"}
        assert self._saved() == []
        assert sent == []
        # And the (single-threaded) test server is serving again.
        assert self._post_files(("after.txt", b"ok"))["ok"] is True

    def test_a_slow_but_steady_upload_is_never_cut_off(self, monkeypatch):
        # The timeout bounds each socket operation, not the request: a large
        # upload over a slow link takes as long as it takes.
        import socket

        monkeypatch.setattr(UploadHandler, "timeout", 0.5)
        data = bytes(range(256)) * 64
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n0\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="s.bin"\r\n\r\n'
            + data
            + b"\r\n------B--\r\n"
        )
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(
                b"POST /upload HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: multipart/form-data; boundary=----B\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
            )
            for i in range(0, len(body), 4096):
                s.sendall(body[i : i + 4096])
                time.sleep(0.2)
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        assert time.monotonic() - started > 0.5, "the upload was not slow enough"
        head, _, payload = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.0 200"), head
        saved = Path(json.loads(payload)["path"])
        assert saved.read_bytes() == data

    def test_a_body_without_its_closing_delimiter_is_refused(self, monkeypatch):
        sent = self._pastes(monkeypatch)
        body = self._CUT[: -len(b"------B--\r\n")]
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 400
        assert json.loads(resp.read()) == {"ok": False, "error": "Upload incomplete"}
        assert self._saved() == []
        assert sent == []

    def test_the_outcome_line_names_every_suffix_and_the_count(self, caplog):
        with caplog.at_level("INFO", logger="magent.upload"):
            reply = self._post_files(("a.zip", b"ZIP"), ("b.py", b"PY"))
            assert reply["ok"] is True
            assert self._wait_log(caplog, "suffix=")
        line = next(
            r.getMessage() for r in caplog.records if "suffix=" in r.getMessage()
        )
        assert "files=2" in line
        assert line.endswith("suffix=.zip,.py")
        assert "a.zip" not in line  # never the original filename

    def test_an_over_limit_upload_names_the_limit(self, monkeypatch):
        import magent.upload_server as mod

        # No envelope allowance: this is the early, declared-length refusal.
        monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 10)
        monkeypatch.setattr(mod, "MULTIPART_ALLOWANCE_BYTES", 0)
        body = b"x" * 64
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 413
        assert json.loads(resp.read()) == {
            "ok": False,
            "error": "File too large - 10 bytes limit",
        }

    def test_every_connection_has_a_per_operation_timeout(self):
        import magent.upload_server as mod

        # Long enough that only a link which has STOPPED is cut off.
        assert UploadHandler.timeout == mod.CONNECTION_TIMEOUT_S
        assert mod.CONNECTION_TIMEOUT_S >= 30

    def test_the_limit_and_the_drain_that_serves_it(self):
        # 100 MB of files, a request ceiling that allows for the envelope, and
        # a reject drain PAST that ceiling: the honest just-over-the-limit
        # client is exactly who the drain exists for (a JSON 413 instead of a
        # Windows RST), and a drain that stopped AT the ceiling would leave the
        # tail of that client's body unread.
        import magent.upload_server as mod

        assert mod.MAX_UPLOAD_BYTES == 100 * 1024 * 1024
        assert mod._request_limit() == (
            mod.MAX_UPLOAD_BYTES + mod.MULTIPART_ALLOWANCE_BYTES
        )
        # Room for a thousand-file selection's part headers.
        assert mod.MULTIPART_ALLOWANCE_BYTES >= 1000 * 300
        assert mod._request_limit() + 1024 * 1024 <= mod._DRAIN_CAP_BYTES

    def test_files_exactly_at_the_limit_land_though_the_request_is_larger(
        self, monkeypatch
    ):
        # The limit is "100 MB of files" -- what the page and the Alt+V
        # listener pre-check. The multipart envelope on top of it used to push
        # a selection they passed into a 413.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 1000)
        reply = self._post_files(("a.bin", b"a" * 400), ("b.bin", b"b" * 600))
        assert reply["ok"] is True
        assert [Path(p).read_bytes() for p in reply["paths"]] == [
            b"a" * 400,
            b"b" * 600,
        ]

    def test_files_one_byte_over_the_limit_are_refused_whole(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 1000)
        reply = self._post_files(("a.bin", b"a" * 400), ("b.bin", b"b" * 601))
        assert reply == {"ok": False, "error": "File too large - 1000 bytes limit"}
        assert self._saved() == []

    def test_no_paste_at_all_is_neither_injected_nor_pending(self):
        # The THIRD state of the reply envelope, on the path a client can reach
        # with no multiplexer installed at all (this fixture's find_psmux is
        # None). Both flags false is what tells a client "no paste happened,
        # and none is coming" -- it is NOT a failed upload, and the page must
        # keep reporting it as the plain "sent" it is. The other two states are
        # pinned in TestASlowPasteNeverBecomesAFailedUpload.
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n1\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="s.png"\r\n\r\n'
            b"FAKEPNG\r\n"
            b"------B--\r\n"
        )
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        data = json.loads(conn.getresponse().read())

        assert data["ok"] is True
        assert data["injected"] is False
        assert data["inject_pending"] is False

    def _wait_log(self, caplog, substr: str, timeout: float = 3.0) -> bool:
        # The outcome INFO logs in the do_POST `finally` block, which runs on
        # the server thread after the HTTP response is already on the wire --
        # same race as the status-line flash (see TestInSessionFeedback), so
        # poll rather than assert immediately.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if substr in caplog.text:
                return True
            time.sleep(0.02)
        return False

    def test_upload_logs_outcome_without_filename(self, caplog):
        # F-hygiene: the outcome log must carry the project + byte-count +
        # injected flag, but never the original filename (personal data).
        body = (
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n'
            b"\r\n"
            b"0\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="my_diagnosis.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"FAKEPNG\r\n"
            b"------WebKitFormBoundary--\r\n"
        )

        with caplog.at_level("INFO", logger="magent.upload"):
            conn = self._conn()
            conn.request(
                "POST",
                "/upload",
                body=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=----WebKitFormBoundary",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["ok"] is True

            assert self._wait_log(caplog, "upload project=marka")
        assert "bytes=7" in caplog.text  # len(b"FAKEPNG")
        assert "injected=False" in caplog.text
        assert "my_diagnosis.png" not in caplog.text
        assert "my_diagnosis" not in caplog.text

    def test_upload_missing_project(self):
        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"\r\n"
            b"data\r\n"
            b"------Boundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["ok"] is False

    def test_upload_rejects_unknown_project(self):
        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"evil-project\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"\r\n"
            b"data\r\n"
            b"------Boundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["ok"] is False
        assert "Unknown project" in data["error"]

    def test_upload_strips_path_traversal(self):
        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n'
            b"\r\n"
            b"0\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="../../etc/passwd"\r\n'
            b"\r\n"
            b"malicious\r\n"
            b"------Boundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["ok"] is True
        saved = Path(data["path"])
        assert saved.parent == self.upload_dir
        assert ".." not in saved.name

    def test_404(self):
        conn = self._conn()
        conn.request("GET", "/nonexistent")
        resp = conn.getresponse()
        assert resp.status == 404

    def test_rejects_oversized_body(self, monkeypatch):
        # F-D3-002: a body over the cap is rejected before it's fully read
        # into memory, so a malicious/oversized upload can't exhaust RAM.
        # P4-02: the reject drains the pending body first, so the client
        # deterministically reads the 413 + JSON envelope instead of a Windows
        # TCP RST ("connection reset") -- no retries, no flake.
        import magent.upload_server as mod

        # No envelope allowance, so this stays the refusal BEFORE the read.
        monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 10)
        monkeypatch.setattr(mod, "MULTIPART_ALLOWANCE_BYTES", 0)

        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"\r\n"
            b"well past ten bytes of file data\r\n"
            b"------Boundary--\r\n"
        )
        assert len(body) > 10  # sanity: genuinely exceeds the lowered cap

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 413
        data = json.loads(resp.read())  # body arrives intact, not a reset
        assert data["ok"] is False
        assert "large" in data["error"].lower()

    def test_rejects_bad_content_length(self):
        # Sibling of the oversized-body guard: a non-numeric Content-Length
        # reaching do_POST used to propagate an uncaught ValueError (dropped
        # connection); it now gets a clean 400 before any body is read.
        # P4-02: the reject drains the (garbage-length) body first, so the
        # client deterministically reads the 400 + JSON envelope rather than a
        # Windows TCP RST -- no retries, no flake.
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=b"irrelevant",
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": "abc",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 400
        data = json.loads(resp.read())  # body arrives intact, not a reset
        assert data["ok"] is False
        assert "Content-Length" in data["error"]

    def test_get_handler_crash_returns_500_and_logs(self, monkeypatch, caplog):
        # P2-03: an unexpected error inside a GET handler must become a clean
        # 500 + an ERROR log record (-> logfile + Sentry), never a dropped
        # connection whose traceback vanishes into socketserver stderr.
        import magent.upload_server as mod

        def boom(_sessions):
            raise RuntimeError("boom in GET")

        monkeypatch.setattr(mod, "_build_html", boom)

        with caplog.at_level("ERROR", logger="magent.upload"):
            conn = self._conn()
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 500
            assert self._wait_log(caplog, "GET handler crashed")

        # the server survived the crash: a normal request still succeeds
        conn2 = self._conn()
        conn2.request("GET", "/api/sessions")
        assert conn2.getresponse().status == 200

    def test_post_handler_crash_returns_500_and_logs(self, monkeypatch, caplog):
        # P2-03: same guarantee for the POST path -- the finding's motivating
        # case (an unexpected fault while handling an upload).
        import magent.upload_server as mod

        def boom(_handler):
            raise RuntimeError("boom in POST")

        monkeypatch.setattr(mod, "_parse_multipart", boom)

        with caplog.at_level("ERROR", logger="magent.upload"):
            conn = self._conn()
            conn.request(
                "POST",
                "/upload",
                body=b"x",
                headers={
                    "Content-Type": "multipart/form-data; boundary=----B",
                    "Content-Length": "1",
                },
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 500
            assert self._wait_log(caplog, "POST handler crashed")

        conn2 = self._conn()
        conn2.request("GET", "/api/sessions")
        assert conn2.getresponse().status == 200


class TestHealth:
    """GET /health proves the handler thread is serving -- a session COUNT,
    never names (hygiene) -- without spawning any psmux subprocess."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: None)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [
            {"name": "marka", "session": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "session": "upup", "path": "INTERNAL/upup"},
        ]
        UploadHandler.sessions_ts = time.time() + 9999
        UploadHandler.port = 8080
        UploadHandler.pid = 4321
        UploadHandler.started_at = time.time() - 5

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def test_health_reports_ok_and_shape(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["ok"] is True
        assert data["service"] == "magent-upload"
        assert data["port"] == 8080
        assert data["pid"] == 4321
        # P3-18: the COUNT is `session_count`; `sessions` is the LIST route.
        assert data["session_count"] == 2
        assert "sessions" not in data
        assert data["uptime_s"] >= 0


class TestHealthSessionCountIsHonest:
    """F4: a fresh serve answered ``session_count: 0`` until the first
    ``/sessions`` request filled the cache -- a false zero for every /health
    consumer. /health may never sweep (it is a liveness probe: cheap, never
    blocked on psmux), so before the first sweep it says ``null`` (unknown),
    and serve warms the cache on a background thread at startup so the unknown
    is brief."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        self.mod = mod
        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        # A never-swept handler: exactly the state of a freshly started serve.
        monkeypatch.setattr(UploadHandler, "cached_sessions", [])
        monkeypatch.setattr(UploadHandler, "sessions_ts", 0)
        monkeypatch.setattr(UploadHandler, "config_path", None)
        monkeypatch.setattr(UploadHandler, "port", 8080)
        monkeypatch.setattr(UploadHandler, "pid", 4321)
        monkeypatch.setattr(UploadHandler, "started_at", time.time() - 5)
        self.sweeps: list[str | None] = []
        self.live = [
            {"name": "marka", "session": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "session": "upup", "path": "INTERNAL/upup"},
        ]

        def _discover(config_path):
            self.sweeps.append(config_path)
            return self.live

        monkeypatch.setattr(mod, "_discover_sessions", _discover)

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        yield
        self.server.shutdown()

    def _health(self) -> dict:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        return json.loads(resp.read())

    def test_a_never_swept_serve_says_unknown_not_zero(self):
        data = self._health()

        assert data["ok"] is True  # liveness is unaffected
        assert data["session_count"] is None
        assert data["sessions_age_s"] is None

    def test_health_never_sweeps(self):
        # Probed constantly; a psmux fan-out per probe would be the cost.
        self._health()
        self._health()

        assert self.sweeps == []

    def test_warming_makes_health_report_the_real_count(self):
        self.mod._warm_sessions()

        data = self._health()

        assert data["session_count"] == 2
        assert 0 <= data["sessions_age_s"] < 5
        assert len(self.sweeps) == 1  # /health added none of its own

    def test_a_real_empty_fleet_is_zero_not_unknown(self):
        self.live = []
        self.mod._warm_sessions()

        assert self._health()["session_count"] == 0

    def test_a_failed_warm_stays_unknown_and_never_raises(self, monkeypatch, caplog):
        def _boom(config_path):
            raise OSError("psmux exploded")

        monkeypatch.setattr(self.mod, "_discover_sessions", _boom)

        with caplog.at_level(logging.WARNING, logger="magent.upload"):
            self.mod._warm_sessions()  # must not propagate: it runs on a daemon thread

        assert self._health()["session_count"] is None
        assert any("session cache" in r.getMessage() for r in caplog.records)

    def test_health_answers_while_a_sweep_is_stuck_in_psmux(self, monkeypatch):
        """The sweep holds the sessions lock for as long as psmux takes. /health
        must not queue behind it."""
        entered = threading.Event()
        release = threading.Event()

        def _slow(config_path):
            entered.set()
            release.wait(10)
            return self.live

        monkeypatch.setattr(self.mod, "_discover_sessions", _slow)
        warm = threading.Thread(target=self.mod._warm_sessions, daemon=True)
        warm.start()
        try:
            assert entered.wait(5), "the warm-up never started its sweep"
            started = time.monotonic()
            data = self._health()
            assert time.monotonic() - started < 2
            assert data["session_count"] is None  # nothing landed yet
        finally:
            release.set()
            warm.join(5)

        assert self._health()["session_count"] == 2

    def test_serve_warms_the_cache_at_startup(self, monkeypatch):
        """End of the wiring: run_server really starts the warm-up."""
        started: list[object] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded_target = target

            def start(self) -> None:
                started.append(self.recorded_target)

        monkeypatch.setattr(self.mod.threading, "Thread", _Recording)
        monkeypatch.setattr(self.mod, "_bind_addresses", lambda _h: ["127.0.0.1"])

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(self.mod, "_NoFqdnHTTPServer", _FakeServer)
        monkeypatch.setattr(
            self.mod, "_pid_path", lambda port: Path(self.mod._UPLOAD_DIR) / "x.pid"
        )

        with pytest.raises(KeyboardInterrupt):
            self.mod.run_server(port=0)

        assert self.mod._warm_sessions in started


class TestInSessionFeedback:
    """Upload progress is flashed into the magent:<project> psmux status line
    -- for the MOBILE page, which has no other screen in that window.

    An Alt+V paste is different: it arrives with ``?project=`` and narrates
    itself (altv.handle_press said "capturing..." before the clipboard was even
    read, and will say the specific outcome when the reply lands). The status
    line is ONE line, so a second voice on it can only race the first, and the
    loser is whichever message the user actually needed. Hence: flagged
    uploads get silence from the server, by design.
    """

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")

        self.calls: list[list[str]] = []

        def _rec(args, **kwargs):
            self.calls.append(list(args))

            class R:
                returncode = 0
                stdout = b""
                stderr = b""

            return R()

        monkeypatch.setattr(mod.subprocess, "run", _rec)
        monkeypatch.setattr(psmux_mod.subprocess, "run", _rec)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [{"name": "marka", "path": "INTERNAL/marka"}]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def _post(
        self, path: str, project_field: str = "marka", filename: str = "c.png"
    ) -> dict:
        body = (
            f"------B\r\n"
            f'Content-Disposition: form-data; name="project"\r\n\r\n'
            f"{project_field}\r\n"
            f"------B\r\n"
            f'Content-Disposition: form-data; name="inject"\r\n\r\n0\r\n'
            f"------B\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'
            f"DATA\r\n"
            f"------B--\r\n"
        ).encode()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        return json.loads(conn.getresponse().read())

    def _flashes(self) -> list[str]:
        return [" ".join(c) for c in self.calls if "display-message" in c]

    def _wait_flash(self, substr: str, timeout: float = 3.0) -> bool:
        # The result flash fires after the HTTP response is sent (so the client
        # isn't blocked on the status-bar subprocess), so poll for it.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(substr in f for f in self._flashes()):
                return True
            time.sleep(0.02)
        return False

    def test_a_mobile_upload_is_confirmed_on_the_bar(self):
        assert self._post("/upload", project_field="marka")["ok"] is True
        assert self._wait_flash("image uploaded")
        # ...at the right session's own socket (a message-style tint may sit
        # between the socket flag and display-message).
        assert any(
            "-L marka" in f and "display-message" in f and "image uploaded" in f
            for f in self._flashes()
        )

    @pytest.mark.parametrize("name", ["shot.PNG", "a.jpeg", "b.webp", "c.bmp"])
    def test_a_single_image_still_says_image(self, name):
        # The phone-screenshot case reads exactly as it always did.
        assert self._post("/upload", filename=name)["ok"] is True
        assert self._wait_flash("image uploaded")

    @pytest.mark.parametrize("name", ["notes.zip", "tool.py", "Makefile"])
    def test_a_single_non_image_says_file(self, name):
        assert self._post("/upload", filename=name)["ok"] is True
        assert self._wait_flash("file uploaded")
        assert not any("image uploaded" in f for f in self._flashes())

    def test_a_mobile_failure_is_shown_too(self):
        assert self._post("/upload", project_field="evil")["ok"] is False
        # An unknown project has no window to flash into; a KNOWN one does.
        assert not self._flashes()

    def test_an_alt_v_upload_gets_no_second_voice_from_the_server(self):
        # Regression pin for the "which message won?" race: ?project= means the
        # listener is already narrating this press, so the server says nothing.
        assert self._post("/upload?project=marka")["ok"] is True
        assert not self._wait_flash("uploaded", timeout=0.6)
        assert not self._wait_flash("uploading image", timeout=0.1)

    def test_several_files_paste_as_one_line_in_one_send(self):
        # An Alt+V press with an Explorer selection sends every file in one
        # request; the pane must get ONE paste carrying all their paths -- not
        # one paste per file racing each other into the input line.
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n1\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.zip"\r\n\r\n'
            b"ZIP\r\n"
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="b.py"\r\n\r\n'
            b"PY\r\n"
            b"------B--\r\n"
        )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/upload?project=marka",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        reply = json.loads(conn.getresponse().read())
        assert reply["ok"] is True and reply["injected"] is True

        from magent.sessions import paths_line

        sends = [c for c in self.calls if "send-keys" in c]
        assert len(sends) == 1, sends
        # Literal (`-l`), like the local paste: the line is text, never a key
        # name -- and it is the whole paste, with no Enter after it.
        verb = sends[0].index("send-keys")
        assert sends[0][verb:] == [
            "send-keys",
            "-t",
            "marka",
            "-l",
            "--",
            paths_line(reply["paths"]),
        ]

    def test_a_mobile_upload_of_several_files_counts_them(self):
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n0\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.zip"\r\n\r\n'
            b"ZIP\r\n"
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="b.py"\r\n\r\n'
            b"PY\r\n"
            b"------B--\r\n"
        )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        assert json.loads(conn.getresponse().read())["ok"] is True
        assert self._wait_flash("2 files uploaded")

    def test_an_alt_v_failure_is_left_to_the_listeners_specific_reason(self):
        # The listener's flash says WHICH failure ("serve said HTTP 400:
        # Unknown project"); a generic "upload failed" from here would stomp it.
        assert self._post("/upload?project=marka", project_field="evil")["ok"] is False
        assert not self._wait_flash("upload failed", timeout=0.6)


class TestASlowPasteNeverBecomesAFailedUpload:
    """The reply must not be hostage to the multiplexer.

    Measured on a live machine: `psmux.send_keys` ran INLINE in this handler
    with no timeout at all, a control command stalled while the session's
    terminal was busy, and the request was answered 74 seconds after the press.
    The listener had given up at 20 s and flashed "upload failed - is `magent
    serve` running?" -- about an image that was already on disk, and that psmux
    went on to paste a minute later. The file being safe is exactly why calling
    that a failure was the damaging part: the user reruns the press, and the
    same screenshot is pasted twice.
    """

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.psmux as psmux_mod
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        # A short grace keeps the test honest AND fast: the assertion is that
        # the reply lands inside whatever the grace is, not that 3s is magic.
        monkeypatch.setattr(mod, "INJECT_GRACE_S", 0.3)

        self.release = threading.Event()
        self.entered = threading.Event()
        self.entered_at = 0.0
        self.worker: threading.Thread | None = None
        self.pastes: list[str] = []

        def _paste(name, *keys, target=None, literal=False, psmux=None, timeout=None):
            # Recorded from INSIDE the worker: the product times its own paste
            # from that thread's clock, and on a loaded runner the thread can
            # start well after the handler's grace has already expired. Tests
            # that want a LATE paste have to synchronize on this, not on the
            # request's return -- see `_stall_past_the_grace`.
            self.pastes.append(name)
            self.entered_at = time.monotonic()
            self.worker = threading.current_thread()
            self.entered.set()
            self.release.wait(20)
            return True

        monkeypatch.setattr(psmux_mod, "send_keys", _paste)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [{"name": "marka", "path": "INTERNAL/marka"}]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.release.set()  # never leave a stalled paste thread behind
        self.server.shutdown()

    def _upload(self) -> tuple[dict, float]:
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n1\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="c.png"\r\n\r\n'
            b"FAKEPNG\r\n"
            b"------B--\r\n"
        )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        started = time.monotonic()
        conn.request(
            "POST",
            "/upload?project=marka",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        data = json.loads(conn.getresponse().read())
        return data, time.monotonic() - started

    def _stall_past_the_grace_then_finish(self) -> None:
        """Release the paste only once it is provably LATE, then join it.

        Both halves close a real race, and the first one is why this test was
        red on a Windows CI runner while passing locally:

        * The product judges lateness against the WORKER's own clock. Releasing
          as soon as the request returns says nothing about that clock -- if the
          thread was slow to be scheduled it starts, returns instantly against
          an already-set event, measures ~1 ms, and correctly logs nothing. The
          poll that followed then waited out its budget against a line that was
          never going to be written. Sleeping to the worker's own deadline is
          the precondition of the assertion, not a guess at a duration.
        * Joining the worker is what makes the log line already WRITTEN when the
          assertion runs: the record is emitted in `_inject_paste`'s `finally`,
          on this thread, after the fake returns. No polling needed.
        """
        import magent.upload_server as mod

        assert self.entered.wait(15), "the paste worker never started"
        # `entered_at` is taken at or after the worker's own `started`, so
        # waiting out the grace from here guarantees the product sees it too.
        # The 50ms margin is not slack: before Python 3.13, Windows'
        # time.monotonic() is GetTickCount64 with 15.6ms granularity, so the
        # worker's final clock read can land a tick short of real time and
        # measure `elapsed` just under the grace -- correctly skipping the
        # late-verdict branch this helper exists to force.
        margin = 0.05
        time.sleep(
            max(0.0, self.entered_at + mod.INJECT_GRACE_S + margin - time.monotonic())
        )
        self.release.set()
        assert self.worker is not None
        self.worker.join(timeout=30)
        assert not self.worker.is_alive(), "the paste worker never finished"

    def test_the_reply_lands_inside_the_grace_and_the_file_is_on_disk(self):
        data, elapsed = self._upload()

        # The stalled paste blocks for 20s. Anything well under that proves the
        # reply is no longer hostage to it; the budget is deliberately loose
        # because a cold runner's cost belongs to the request, not the fix.
        assert elapsed < 5.0, f"the reply waited {elapsed:.2f}s on a stalled paste"
        # ok=True is the whole point: the bytes ARE stored.
        assert data["ok"] is True
        assert Path(data["path"]).read_bytes() == b"FAKEPNG"

    def test_a_stalled_paste_is_reported_as_pending_not_as_a_refusal(self):
        data, _ = self._upload()
        # Three states, not two. `injected: false` alone is indistinguishable
        # from "psmux said no", which is what the bar rendered as a failure.
        assert data["injected"] is False
        assert data["inject_pending"] is True

    def test_a_paste_that_lands_in_time_is_plainly_injected(self, monkeypatch):
        # The mirror race: with a 0.3s grace, a worker thread that is merely
        # SLOW TO BE SCHEDULED on a loaded runner would be reported pending
        # even though the paste itself is instant. The grace is a ceiling, not
        # a delay -- the handler returns the moment the worker does -- so a
        # generous one costs this test nothing and removes the flake.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "INJECT_GRACE_S", 30.0)
        self.release.set()
        data, elapsed = self._upload()
        assert data["injected"] is True
        assert data["inject_pending"] is False
        # ...and it really returned on the paste, not on the ceiling.
        assert elapsed < 10.0, f"the reply took {elapsed:.2f}s on an instant paste"

    def test_the_stalled_paste_is_still_running_and_is_never_re_sent(self):
        # One attempt, ever. A `send-keys` that is merely slow is still in
        # flight; a retry on top of it pastes the same image twice.
        self._upload()
        assert self.entered.wait(15), "the paste worker never started"
        assert self.pastes == ["marka"]
        # ...and letting the (single) attempt run to completion adds no second
        # one -- a retry would have to happen after this point to exist at all.
        self._stall_past_the_grace_then_finish()
        assert self.pastes == ["marka"]

    def test_the_late_verdict_reaches_the_log_since_it_cannot_reach_the_bar(
        self, caplog
    ):
        # A flagged (?project=) upload has a narrator already and the server
        # must not become a second one -- so the deferred result is recorded
        # here instead. Silence would make "did it ever paste?" unanswerable.
        with caplog.at_level(logging.WARNING, logger="magent.upload"):
            self._upload()
            self._stall_past_the_grace_then_finish()
            assert "finished late" in caplog.text
            assert "marka" in caplog.text

    def test_the_pending_flag_is_in_the_outcome_log_line(self, caplog):
        # This one is written on the SERVER thread, in do_POST's `finally`,
        # after the response is already on the wire -- the same documented
        # race `_wait_log` exists for above, so it polls rather than joins.
        with caplog.at_level(logging.INFO, logger="magent.upload"):
            self._upload()
            deadline = time.time() + 10
            while time.time() < deadline and "pending=True" not in caplog.text:
                time.sleep(0.02)
            assert "pending=True" in caplog.text


class TestFlashEndpoint:
    """GET /api/flash -- the on-screen voice of callers that have no screen.
    The Alt+V/F2 listener runs hidden with no terminal, so this route is the
    only way an F2 failure reaches the user instead of only hotkey.log."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.psmux as psmux_mod
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")

        self.calls: list[list[str]] = []

        def _rec(args, **kwargs):
            self.calls.append(list(args))

            class R:
                returncode = 0
                stdout = b""
                stderr = b""

            return R()

        monkeypatch.setattr(psmux_mod.subprocess, "run", _rec)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [{"name": "marka", "path": "INTERNAL/marka"}]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def _get(self, path: str):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def _flashes(self) -> list[list[str]]:
        return [c for c in self.calls if "display-message" in c]

    def test_flashes_the_decoded_message_at_the_named_project(self):
        status, data = self._get("/api/flash?project=marka&msg=F2%3A%20opening...")
        assert status == 200
        assert data == {"ok": True}
        flash = self._flashes()[0]
        # Routed to that project's own psmux socket, with the message decoded.
        assert flash[:3] == ["psmux", "-L", "marka"]
        assert flash[-1] == "F2: opening..."

    def test_plus_and_percent_escapes_are_decoded(self):
        # quote() emits %20 for spaces, but a hand-built or browser-issued URL
        # can use "+" -- parse_qs decodes both, and neither may leak literally.
        self._get("/api/flash?project=marka&msg=a+b%20c%26d")
        assert self._flashes()[0][-1] == "a b c&d"

    def test_long_messages_are_clamped_server_side(self):
        from magent.sessions import FLASH_MSG_MAX

        self._get(f"/api/flash?project=marka&msg={'z' * (FLASH_MSG_MAX + 200)}")
        # Clamped independently of the client: a status bar is one line wide.
        assert self._flashes()[0][-1] == "z" * FLASH_MSG_MAX

    def test_missing_msg_is_400_and_flashes_nothing(self):
        status, data = self._get("/api/flash?project=marka")
        assert status == 400
        assert data["ok"] is False
        assert data["error"]
        assert self._flashes() == []

    def test_missing_project_is_400_and_flashes_nothing(self):
        status, data = self._get("/api/flash?msg=hello")
        assert status == 400
        assert data["ok"] is False
        assert self._flashes() == []

    def test_no_query_at_all_is_400(self):
        status, data = self._get("/api/flash")
        assert status == 400
        assert data["ok"] is False

    def test_a_phase_message_can_ask_to_linger(self):
        # A phase ("uploading...") that expires while the step is still running
        # leaves a blank bar, which reads exactly like the silence this route
        # exists to end -- so the caller may set its own duration.
        self._get("/api/flash?project=marka&msg=working&ms=20000")
        assert "20000" in self._flashes()[0]

    def test_an_absurd_or_broken_duration_is_clamped_not_obeyed(self):
        import magent.upload_server as mod

        self._get("/api/flash?project=marka&msg=a&ms=99999999")
        self._get("/api/flash?project=marka&msg=b&ms=notanumber")
        self._get("/api/flash?project=marka&msg=c&ms=-5")
        durations = [f[f.index("-d") + 1] for f in self._flashes()]
        assert durations == [
            str(mod._FLASH_MSG_MS_MAX),
            str(mod._FLASH_MSG_MS),  # unparseable falls back, never fails the flash
            str(mod._FLASH_MSG_MS_MIN),
        ]

    def test_the_tint_reaches_the_message_style(self):
        # psmux's message-style is GLOBAL on the socket, so the caller sets it
        # on every message; err must not leak into the next ok (and vice versa).
        import magent.upload_server as mod

        self._get("/api/flash?project=marka&msg=bad&tint=err")
        self._get("/api/flash?project=marka&msg=fine&tint=ok")
        styled = [c for c in self.calls if "message-style" in c]
        assert mod._MSG_RED in styled[0]
        assert mod._MSG_GREEN in styled[1]

    def test_an_unknown_tint_leaves_the_style_alone_and_still_flashes(self):
        self._get("/api/flash?project=marka&msg=hello&tint=chartreuse")
        assert self._flashes()[0][-1] == "hello"
        assert not any("message-style" in c for c in self.calls)

    def test_post_on_the_flash_route_is_405_not_404(self):
        # P3-16: /api/flash is a real GET route, so the wrong verb answers 405.
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/flash", body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        assert resp.status == 405
        assert json.loads(resp.read())["ok"] is False


class TestStopServer:
    """Truthful stop_server: True only when the kill actually succeeded; the
    pid file survives a failed kill so `status`/a retry can still find it."""

    def test_no_pid_file_returns_false(self, tmp_path, monkeypatch):
        # Pin: this invariant is unchanged by the taskkill-rc behavior below.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_pid_path", lambda port: tmp_path / "nonexistent.pid")
        assert mod.stop_server(9999) is False

    def test_keeps_pid_file_when_taskkill_fails(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        pid_file = tmp_path / "upload_server-9999.pid"
        pid_file.write_text("4321")
        monkeypatch.setattr(mod, "_pid_path", lambda port: pid_file)
        monkeypatch.setattr(mod.sys, "platform", "win32")

        class _Result:
            returncode = 1

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        assert mod.stop_server(9999) is False
        assert pid_file.exists()

    def test_removes_pid_file_when_taskkill_succeeds(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        pid_file = tmp_path / "upload_server-9999.pid"
        pid_file.write_text("4321")
        monkeypatch.setattr(mod, "_pid_path", lambda port: pid_file)
        monkeypatch.setattr(mod.sys, "platform", "win32")

        class _Result:
            returncode = 0

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        assert mod.stop_server(9999) is True
        assert not pid_file.exists()


class TestServerPidAcrossARestart:
    """A restart leaves upload_server-<port>.pid behind and the OS reuses pid
    numbers. server_pid is what `status` (DEAD vs off), `stop_server` (what to
    taskkill) and the phone-URL port pick all read, so a recycled pid there was
    a DEAD upload server after every reboot -- and a `down --all` that killed
    whatever process now wore the old number."""

    def _pre_boot(self, tmp_path, monkeypatch, *, boot=5000.0):
        import os

        import magent.upload_server as mod

        pid_file = tmp_path / "upload_server-9999.pid"
        pid_file.write_text("4321")
        os.utime(pid_file, (1000.0, 1000.0))
        monkeypatch.setattr(mod, "_pid_path", lambda port: pid_file)
        monkeypatch.setattr("magent.procs.boot_time", lambda: boot)
        return mod, pid_file

    def test_a_pid_file_from_before_the_boot_names_no_server(
        self, tmp_path, monkeypatch
    ):
        mod, pid_file = self._pre_boot(tmp_path, monkeypatch)

        assert mod.server_pid(9999) is None
        assert not pid_file.exists()  # nothing of ours: cleared, like the others

    def test_a_pid_file_written_since_the_boot_is_read_as_before(
        self, tmp_path, monkeypatch
    ):
        mod, pid_file = self._pre_boot(tmp_path, monkeypatch, boot=500.0)

        assert mod.server_pid(9999) == 4321
        assert pid_file.exists()

    def test_an_unknown_boot_time_changes_nothing(self, tmp_path, monkeypatch):
        mod, _pid_file = self._pre_boot(tmp_path, monkeypatch)
        monkeypatch.setattr("magent.procs.boot_time", lambda: None)

        assert mod.server_pid(9999) == 4321

    def test_stop_never_kills_a_pid_recorded_before_the_boot(
        self, tmp_path, monkeypatch
    ):
        mod, _pid_file = self._pre_boot(tmp_path, monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        calls = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(a))

        assert mod.stop_server(9999) is False
        assert calls == []


class TestBindAddresses:
    """R7: the upload server must never bind the LAN wildcard 0.0.0.0 --
    only loopback (so the cli.py liveness probe + localhost URL keep
    working) plus the Tailscale IP when one is available."""

    def test_auto_bind_loopback_only_when_no_tailscale(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: None)
        assert mod._bind_addresses(None) == ["127.0.0.1"]
        assert "0.0.0.0" not in mod._bind_addresses(None)

    def test_auto_bind_includes_tailscale(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: "100.64.1.2")
        assert mod._bind_addresses(None) == ["127.0.0.1", "100.64.1.2"]

    def test_explicit_host_honored(self):
        import magent.upload_server as mod

        # The --host escape hatch is honored verbatim, including 0.0.0.0.
        assert mod._bind_addresses("0.0.0.0") == ["0.0.0.0"]

    def test_run_server_binds_expected(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: None)
        monkeypatch.setattr(
            mod, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )

        constructed = []

        class _FakeServer:
            def __init__(self, address, handler_cls):
                self.server_address = address
                constructed.append(address)

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

            def server_close(self):
                pass

        # run_server constructs _NoFqdnHTTPServer (the no-reverse-DNS subclass).
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _FakeServer)
        # ...and it now also supervises the Alt+V listener, which spawns a
        # process that installs a SYSTEM-WIDE keyboard hook. Never from a unit
        # test: the wiring is pinned separately, with a stub.
        supervised = []
        monkeypatch.setattr(
            mod, "_supervise_hotkey", lambda url, stop, **kw: supervised.append(url)
        )

        with pytest.raises(KeyboardInterrupt):
            mod.run_server(port=0)

        assert constructed == [("127.0.0.1", 0)]  # loopback only, never 0.0.0.0
        assert supervised == ["http://127.0.0.1:0"]  # the listener gets an owner

    def test_server_bind_never_reverse_resolves(self, monkeypatch):
        """Pin the macOS-wedge fix: server_bind must not call socket.getfqdn.

        HTTPServer.server_bind's getfqdn(host) goes through mDNSResponder on
        macOS and was observed blocking forever on CI -- socket bound, listen()
        never reached, clients hanging. _NoFqdnHTTPServer records the bind host
        verbatim; a regression back to the stdlib bind trips the bomb below.
        """
        import socket

        import magent.upload_server as mod

        def _bomb(name: str = "") -> str:
            raise AssertionError(
                "server_bind must never reverse-resolve (macOS mdns wedge)"
            )

        monkeypatch.setattr(socket, "getfqdn", _bomb)
        srv = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        try:
            assert srv.server_name == "127.0.0.1"
            assert srv.server_port == srv.server_address[1]
            assert srv.server_port != 0
        finally:
            srv.server_close()


class TestOnePortOneServer:
    """A second ``magent serve`` on a port one already holds must FAIL to bind.

    The defect: ``ThreadingHTTPServer`` sets ``allow_reuse_address``, i.e.
    SO_REUSEADDR, and on Windows that option lets a second process bind a port
    that is already LISTENING. Measured: two live servers on one port, both
    logging ``listening ... :15505``, the pid file naming only the later one --
    so the watchdog killed or revived the wrong one and ``/health`` was answered
    by whichever server the kernel happened to pick. Linux SO_REUSEADDR never
    allowed two live listeners on the same address, which is why only Windows
    ever showed it.

    Every port here is ephemeral (bind 0, then reuse what the kernel handed
    out); a real ``magent serve`` port is never touched.
    """

    def test_a_second_server_on_a_held_port_is_refused(self):
        import magent.upload_server as mod

        first = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        try:
            port = first.server_address[1]
            with pytest.raises(OSError) as refused:
                second = mod._NoFqdnHTTPServer(("127.0.0.1", port), mod.UploadHandler)
                second.server_close()  # only reached on the regression
            assert mod._port_taken(refused.value)
        finally:
            first.server_close()

    @pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE")
    def test_windows_claims_the_port_exclusively(self):
        import socket

        import magent.upload_server as mod

        srv = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        try:
            opt = srv.socket.getsockopt
            assert opt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
            assert opt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        finally:
            srv.server_close()

    @pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE")
    def test_windows_refuses_a_foreign_reuseaddr_socket_too(self):
        """A regression guard, not the exclusivity pin (the getsockopt test above
        is): this build of Windows refuses a same-address SO_REUSEADDR socket
        -- the stdlib default -- even against a plain holder. Guards that the
        server never regresses to a holder such a socket CAN join."""
        import socket

        import magent.upload_server as mod

        srv = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                thief.bind(("127.0.0.1", srv.server_address[1]))
        finally:
            thief.close()
            srv.server_close()

    def test_the_options_are_set_before_the_bind(self, monkeypatch):
        """Microsoft: SO_EXCLUSIVEADDRUSE only works if set BEFORE bind. After
        it, a loopback-only test cannot tell the difference -- so pin the order
        by reading the socket at the moment the stdlib bind runs."""
        import socket
        import socketserver

        import magent.upload_server as mod

        seen = []
        stdlib_bind = socketserver.TCPServer.server_bind

        def _bind(self):
            opt = self.socket.getsockopt
            if sys.platform == "win32":
                seen.append(opt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE))
            else:
                seen.append(opt(socket.SOL_SOCKET, socket.SO_REUSEADDR))
            stdlib_bind(self)

        monkeypatch.setattr(socketserver.TCPServer, "server_bind", _bind)
        srv = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        srv.server_close()
        assert len(seen) == 1
        assert seen[0] != 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX keeps SO_REUSEADDR")
    def test_posix_keeps_reuseaddr(self):
        import socket

        import magent.upload_server as mod

        srv = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        try:
            assert srv.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        finally:
            srv.server_close()

    def test_a_restart_right_after_serving_traffic_rebinds_the_port(self):
        """The reason SO_REUSEADDR existed at all. The server closes each
        connection first, so its side sits in TIME_WAIT after every request:
        POSIX refuses the rebind without SO_REUSEADDR, and Windows must keep
        rebinding with SO_EXCLUSIVEADDRUSE (an upgrade restarts serve at once).
        """
        import urllib.request
        from http.server import BaseHTTPRequestHandler

        import magent.upload_server as mod

        class _Ok(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        first = mod._NoFqdnHTTPServer(("127.0.0.1", 0), _Ok)
        port = first.server_address[1]
        loop = threading.Thread(target=first.serve_forever, daemon=True)
        loop.start()
        try:
            for _ in range(5):
                url = f"http://127.0.0.1:{port}/"
                with urllib.request.urlopen(url, timeout=10) as resp:
                    assert resp.read() == b"ok"
        finally:
            first.shutdown()
            first.server_close()
            loop.join(timeout=10)

        again = mod._NoFqdnHTTPServer(("127.0.0.1", port), _Ok)
        again.server_close()

    def test_the_stdlib_default_is_switched_off(self):
        """TCPServer.server_bind adds SO_REUSEADDR itself when this is true --
        on Windows that alone re-opens the double bind."""
        import magent.upload_server as mod

        assert mod._NoFqdnHTTPServer.allow_reuse_address is False


class _RecordingSocket:
    def __init__(self):
        self.options = []

    def setsockopt(self, level, name, value):
        self.options.append((level, name, value))


class TestClaimPortOptions:
    """The per-OS bind policy, driven with a fake socket so BOTH branches run
    on every OS. The real-socket half is TestOnePortOneServer."""

    def test_windows_sets_exclusive_and_never_reuseaddr(self):
        import socket

        import magent.upload_server as mod

        sock = _RecordingSocket()
        mod._claim_port_options(sock, platform="win32")
        assert sock.options == [(socket.SOL_SOCKET, -5, 1)]
        assert not [o for o in sock.options if o[1] == socket.SO_REUSEADDR]

    def test_the_exclusive_constant_is_winsocks(self):
        import socket

        import magent.upload_server as mod

        if sys.platform == "win32":
            assert mod._SO_EXCLUSIVEADDRUSE == socket.SO_EXCLUSIVEADDRUSE
        assert mod._SO_EXCLUSIVEADDRUSE == -5  # ((int)(~SO_REUSEADDR)), SO_REUSEADDR=4

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_posix_sets_reuseaddr_only(self, platform):
        import socket

        import magent.upload_server as mod

        sock = _RecordingSocket()
        mod._claim_port_options(sock, platform=platform)
        assert sock.options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


class _WinError(OSError):
    """An OSError carrying a Windows socket code, constructible on every OS."""

    def __init__(self, err, winerror):
        super().__init__(err, "refused")
        self.winerror = winerror


class TestPortTaken:
    def test_addr_in_use_is_taken(self):
        import errno

        import magent.upload_server as mod

        assert mod._port_taken(OSError(errno.EADDRINUSE, "Address already in use"))

    def test_wsaeacces_is_ambiguous_not_taken(self):
        """Measured: an exclusive wildcard holder refuses loopback with
        WSAEACCES -- and so does a Windows-reserved port with no holder at all
        (127.0.0.1:17000). The code alone cannot say "taken"."""
        import errno

        import magent.upload_server as mod

        exc = _WinError(errno.EACCES, 10013)
        assert not mod._port_taken(exc)
        assert mod._access_refused(exc, platform="win32")

    def test_eacces_on_posix_is_a_privileged_port_not_a_holder(self):
        import errno

        import magent.upload_server as mod

        exc = OSError(errno.EACCES, "denied")
        assert not mod._port_taken(exc)
        assert not mod._access_refused(exc, platform="linux")

    def test_an_address_that_is_not_ours_is_not_taken(self):
        """A Tailscale IP that went away keeps the degraded path it always had."""
        import errno

        import magent.upload_server as mod

        exc = OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        assert not mod._port_taken(exc)
        assert not mod._access_refused(exc, platform="win32")


class TestHolderAnswers:
    """The probe that splits WSAEACCES into held and reserved. Real loopback
    sockets on ephemeral ports only; nothing binds the wildcard."""

    def test_a_listening_port_answers(self):
        import socket

        import magent.upload_server as mod

        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", 0))
            holder.listen(8)  # room for both probes; nothing ever accepts
            port = holder.getsockname()[1]
            assert mod._holder_answers("127.0.0.1", port)
            # A wildcard refusal is asked on loopback, where the holder is.
            assert mod._holder_answers("0.0.0.0", port)
        finally:
            holder.close()

    def test_a_port_nobody_listens_on_does_not(self):
        import socket

        import magent.upload_server as mod

        spare = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        spare.bind(("127.0.0.1", 0))  # bound, never listening: nothing accepts
        try:
            assert not mod._holder_answers("127.0.0.1", spare.getsockname()[1])
        finally:
            spare.close()


class TestRunServerOnAHeldPort:
    """A second serve must END, quickly and by name, and leave the holder's
    port and pid file exactly as it found them."""

    def _isolate(self, mod, monkeypatch, tmp_path, addrs):
        monkeypatch.setattr(mod, "_bind_addresses", lambda host: list(addrs))
        monkeypatch.setattr(
            mod, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )
        # Never install a system-wide keyboard hook from a unit test.
        monkeypatch.setattr(
            mod,
            "_supervise_hotkey",
            lambda *a, **kw: pytest.fail("a server that never bound supervised"),
        )

    def test_a_real_held_port_ends_the_second_server_by_name(
        self, tmp_path, monkeypatch, caplog
    ):
        import magent.upload_server as mod

        holder = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        try:
            port = holder.server_address[1]
            self._isolate(mod, monkeypatch, tmp_path, ["127.0.0.1"])
            # On the regression the second bind SUCCEEDS and run_server would
            # serve forever; fail instead of hanging the run.
            monkeypatch.setattr(
                mod._NoFqdnHTTPServer,
                "serve_forever",
                lambda self, *a, **kw: pytest.fail("a second server bound the port"),
            )
            pid_file = tmp_path / f"upload-{port}.pid"
            pid_file.write_text("424242")  # the holder's record

            with (
                caplog.at_level("INFO", logger="magent.upload"),
                pytest.raises(mod.PortInUse),
            ):
                mod.run_server(port=port)

            assert pid_file.read_text() == "424242"
            assert "already in use" in caplog.text
            # A held port is not a reserved one; that wording is the other path.
            assert "reserved" not in caplog.text
            assert "listening" not in caplog.text
            # Losing a race is the designed outcome, not a crash for Sentry.
            assert not [r for r in caplog.records if r.levelname == "ERROR"]
        finally:
            holder.server_close()

    def test_a_held_secondary_address_gives_back_the_loopback_bind(
        self, tmp_path, monkeypatch
    ):
        """Serving the addresses that were free would be two servers and one pid
        file again -- so what was bound is closed and nothing serves."""
        import errno

        import magent.upload_server as mod

        closed = []

        class _Server:
            def __init__(self, address, handler_cls):
                if address[0] == "100.64.1.2":
                    raise OSError(errno.EADDRINUSE, "Address already in use")
                self.server_address = address

            def serve_forever(self):
                pytest.fail("a refused server must never serve")

            def server_close(self):
                closed.append(self.server_address)

        self._isolate(mod, monkeypatch, tmp_path, ["127.0.0.1", "100.64.1.2"])
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _Server)

        with pytest.raises(mod.PortInUse):
            mod.run_server(port=8034)

        assert closed == [("127.0.0.1", 8034)]
        assert not (tmp_path / "upload-8034.pid").exists()

    def test_an_unavailable_secondary_address_still_degrades(
        self, tmp_path, monkeypatch, caplog
    ):
        """Unchanged: a Tailscale IP that cannot be bound for any OTHER reason
        is a warning, and loopback keeps serving."""
        import errno

        import magent.upload_server as mod

        class _Server:
            def __init__(self, address, handler_cls):
                if address[0] == "100.64.1.2":
                    raise OSError(errno.EADDRNOTAVAIL, "Cannot assign")
                self.server_address = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

            def server_close(self):
                pass

        self._isolate(mod, monkeypatch, tmp_path, ["127.0.0.1", "100.64.1.2"])
        monkeypatch.setattr(mod, "_supervise_hotkey", lambda url, stop, **kw: None)
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _Server)

        with (
            caplog.at_level("INFO", logger="magent.upload"),
            pytest.raises(KeyboardInterrupt),
        ):
            mod.run_server(port=8034)

        assert "cannot bind 100.64.1.2:8034" in caplog.text
        assert "listening on 127.0.0.1:8034" in caplog.text

    def _refuse_with_wsaeacces(self, mod, monkeypatch, refused_addr, answers):
        """Drive the WSAEACCES branch on every OS: the fake raises winerror
        10013 for ``refused_addr``, the real classifier is pinned to win32, and
        the holder probe is a recorded stub answering ``answers``."""
        import errno
        import functools

        probed = []

        class _Server:
            def __init__(self, address, handler_cls):
                if address[0] == refused_addr:
                    raise _WinError(errno.EACCES, 10013)
                self.server_address = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

            def server_close(self):
                pass

        def _probe(addr, port):
            probed.append((addr, port))
            return answers

        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _Server)
        monkeypatch.setattr(
            mod,
            "_access_refused",
            functools.partial(mod._access_refused, platform="win32"),
        )
        monkeypatch.setattr(mod, "_holder_answers", _probe)
        return probed

    def test_wsaeacces_with_a_holder_answering_is_a_held_port(
        self, tmp_path, monkeypatch, caplog
    ):
        import magent.upload_server as mod

        self._isolate(mod, monkeypatch, tmp_path, ["127.0.0.1"])
        probed = self._refuse_with_wsaeacces(mod, monkeypatch, "127.0.0.1", True)

        with (
            caplog.at_level("INFO", logger="magent.upload"),
            pytest.raises(mod.PortInUse),
        ):
            mod.run_server(port=8034)

        assert probed == [("127.0.0.1", 8034)]
        assert "already in use" in caplog.text
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_wsaeacces_with_nobody_answering_is_a_reserved_port(
        self, tmp_path, monkeypatch, caplog
    ):
        """No holder means no "second server" and nobody to defer to: a serve
        that can never start is a fatal ERROR (what Sentry captures) that names
        the reservation and where to look, not a lost race at WARNING."""
        import magent.upload_server as mod

        self._isolate(mod, monkeypatch, tmp_path, ["127.0.0.1"])
        probed = self._refuse_with_wsaeacces(mod, monkeypatch, "127.0.0.1", False)

        with (
            caplog.at_level("INFO", logger="magent.upload"),
            pytest.raises(mod.BindFailed) as failed,
        ):
            mod.run_server(port=8034)

        assert not isinstance(failed.value, mod.PortInUse)
        assert probed == [("127.0.0.1", 8034)]
        message = str(failed.value)
        assert "port 8034 is reserved or not permitted on 127.0.0.1" in message
        assert "excludedportrange" in message
        assert "second server" not in caplog.text
        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert errors == [message]
        assert not (tmp_path / "upload-8034.pid").exists()

    def test_a_reserved_secondary_address_degrades_like_any_other(
        self, tmp_path, monkeypatch, caplog
    ):
        import magent.upload_server as mod

        self._isolate(mod, monkeypatch, tmp_path, ["127.0.0.1", "100.64.1.2"])
        monkeypatch.setattr(mod, "_supervise_hotkey", lambda url, stop, **kw: None)
        probed = self._refuse_with_wsaeacces(mod, monkeypatch, "100.64.1.2", False)

        with (
            caplog.at_level("INFO", logger="magent.upload"),
            pytest.raises(KeyboardInterrupt),
        ):
            mod.run_server(port=8034)

        assert probed == [("100.64.1.2", 8034)]
        assert "reserved or not permitted on 100.64.1.2" in caplog.text
        assert "listening on 127.0.0.1:8034" in caplog.text


class TestLocalUrl:
    """Which URL the supervised listener is handed. It must be reachable from
    THIS machine without Tailscale being up -- the listener posts every Alt+V
    image through it."""

    def test_default_bind_uses_loopback(self):
        import magent.upload_server as mod

        assert mod.local_url(["127.0.0.1", "100.64.0.1"], 8034) == (
            "http://127.0.0.1:8034"
        )

    def test_lan_wildcard_still_resolves_to_loopback(self):
        # `serve --host 0.0.0.0` binds loopback too; "http://0.0.0.0:..." is
        # not a URL a client should be handed.
        import magent.upload_server as mod

        assert mod.local_url(["0.0.0.0"], 8034) == "http://127.0.0.1:8034"


class TestCrashVisibility:
    """serve died silently twice in one day and left NOTHING behind: no
    traceback (a detached process has no console), no log line, only a pid file
    whose process was gone. Every exit now names its reason, and a crash is
    logged at exception level -- which is also what hands it to Sentry
    (errors-only, logging integration at ERROR). Nothing is swallowed.
    """

    def _fake_server(self, fail: BaseException | None):
        class _FakeServer:
            def __init__(self, address, handler_cls):
                self.server_address = address

            def serve_forever(self):
                if fail is not None:
                    raise fail

            def shutdown(self):
                pass

            def server_close(self):
                pass

        return _FakeServer

    def _run(self, mod, monkeypatch, tmp_path, fail):
        monkeypatch.setattr(mod.tailnet, "ip4", lambda: None)
        monkeypatch.setattr(
            mod, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", self._fake_server(fail))
        # Never install a system-wide keyboard hook from a unit test.
        monkeypatch.setattr(mod, "_supervise_hotkey", lambda url, stop, **kw: None)
        mod.run_server(port=0)

    def test_a_clean_stop_logs_one_line_naming_the_reason(
        self, tmp_path, monkeypatch, caplog
    ):
        import magent.upload_server as mod

        with (
            caplog.at_level("INFO", logger="magent.upload"),
            pytest.raises(KeyboardInterrupt),
        ):
            self._run(mod, monkeypatch, tmp_path, KeyboardInterrupt())

        assert "stopped: keyboard interrupt" in caplog.text
        # A clean stop is not an error -- nothing for Sentry to capture.
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_a_crash_is_logged_at_exception_level_and_still_propagates(
        self, tmp_path, monkeypatch, caplog
    ):
        import magent.upload_server as mod

        boom = RuntimeError("accept loop exploded")
        with (
            caplog.at_level("INFO", logger="magent.upload"),
            pytest.raises(RuntimeError),
        ):
            self._run(mod, monkeypatch, tmp_path, boom)

        crashes = [r for r in caplog.records if r.levelname == "ERROR"]
        assert crashes, "a fatal serve exception must reach the log"
        assert crashes[0].exc_info is not None  # log.exception, not log.error
        assert "upload server crashed" in caplog.text
        assert "stopped: crashed" in caplog.text

    def test_no_bindable_address_is_an_error_line_not_just_a_traceback(
        self, tmp_path, monkeypatch, caplog
    ):
        import magent.upload_server as mod

        class _Unbindable:
            def __init__(self, address, handler_cls):
                raise OSError("address in use")

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: None)
        monkeypatch.setattr(
            mod, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _Unbindable)

        with (
            caplog.at_level("ERROR", logger="magent.upload"),
            pytest.raises(RuntimeError),
        ):
            mod.run_server(port=0)

        assert "no bindable address" in caplog.text

    def test_a_secondary_bind_crash_is_logged_and_never_re_raised(self, caplog):
        """The Tailscale bind runs on its own daemon thread. Its death must not
        take the loopback bind with it, but it must not be silent either."""
        import logging

        import magent.upload_server as mod

        class _Dying:
            server_address = ("100.64.1.2", 8034)

            def serve_forever(self):
                raise OSError("interface went away")

        with caplog.at_level("ERROR", logger="magent.upload"):
            mod._serve_bind(_Dying(), logging.getLogger("magent.upload"))

        assert "stopped serving" in caplog.text

    def test_a_bind_that_excluded_loopback_uses_what_was_bound(self):
        # `serve --host <tailscale-ip>`: loopback is genuinely not listening,
        # so claiming it would hand the listener a dead URL.
        import magent.upload_server as mod

        assert mod.local_url(["100.64.0.1"], 8034) == "http://100.64.0.1:8034"


class _Env:
    """Stand-in for MagentEnv over the fields this code path reads: the
    supervisor's own switch, plus log_level (get_logger consults it)."""

    log_level = None

    def __init__(self, hotkey_supervisor: bool) -> None:
        self.hotkey_supervisor = hotkey_supervisor


class TestHotkeySupervisor:
    """serve owns the Alt+V listener's liveness.

    Before this, the listener was a one-shot spawn by whichever launch/attach
    ran last: a reboot or a crash left Alt+V dead with nothing ever re-checking
    it, and `status` reported that as a benign default.
    """

    def _mod(self):
        import magent.upload_server as mod

        return mod

    def _fake_platform(self, monkeypatch, *, supports_hotkey):
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_hotkey=supports_hotkey)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

    def _ensure(self, monkeypatch, result=4242):
        calls: list[str] = []
        self.watches: list[object] = []

        def _fake(url, watch=None):
            calls.append(url)
            self.watches.append(watch)
            return result

        monkeypatch.setattr("magent.launch.ensure_hotkey_listener", _fake)
        return calls

    def test_checks_immediately_and_then_every_interval(self, monkeypatch):
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)
        stop = threading.Event()
        waits: list[float] = []

        def _wait(timeout):
            waits.append(timeout)
            return len(waits) >= 3  # stop on the third pass

        monkeypatch.setattr(stop, "wait", _wait)

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=30.0)

        # The first check is NOT deferred by an interval: a serve that just
        # started must not leave Alt+V dead for 30 more seconds.
        assert calls == ["http://127.0.0.1:8034"] * 3
        assert waits == [30.0, 30.0, 30.0]

    def test_one_watch_rides_every_pass(self, monkeypatch):
        # The wedge confirm and the replacement cooldown live on the watch, so
        # a fresh one per pass would forget both and could never confirm a
        # wedge across two ticks.
        from magent.launch import ListenerWatch

        self._fake_platform(monkeypatch, supports_hotkey=True)
        self._ensure(monkeypatch)
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: len(self.watches) >= 3)

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert len(self.watches) == 3
        assert isinstance(self.watches[0], ListenerWatch)
        assert self.watches[1] is self.watches[0] is self.watches[2]

    def test_the_env_opt_out_stops_it_before_it_touches_anything(self, monkeypatch):
        # MAGENT_HOTKEY_SUPERVISOR=0 is what keeps a test that starts a real
        # serve from installing a system-wide keyboard hook on a dev machine.
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)
        monkeypatch.setattr("magent.env.get_env", lambda: _Env(False))

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", threading.Event())

        assert calls == []

    def test_a_broken_environment_still_supervises(self, monkeypatch, caplog):
        # A detached daemon must not lose a feature because some unrelated
        # MAGENT_* var went bad after it started (same posture as log.py).
        from pydantic import ValidationError

        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)

        def _bad():
            raise ValidationError.from_exception_data("MagentEnv", [])

        monkeypatch.setattr("magent.env.get_env", _bad)
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: True)

        with caplog.at_level("WARNING", logger="magent.hotkey"):
            self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert calls == ["http://127.0.0.1:8034"]
        assert "did not validate" in caplog.text

    def test_returns_immediately_where_the_platform_has_no_hotkey(self, monkeypatch):
        self._fake_platform(monkeypatch, supports_hotkey=False)
        calls = self._ensure(monkeypatch)

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", threading.Event())

        assert calls == []  # and no unbounded loop on a non-Windows serve

    def test_a_failed_spawn_is_logged_and_retried_not_fatal(self, monkeypatch, caplog):
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch, result=None)  # child never confirmed
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: len(calls) >= 2)

        with caplog.at_level("WARNING", logger="magent.hotkey"):
            self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert len(calls) == 2  # it tried again rather than giving up
        assert "no Alt+V listener came up" in caplog.text

    def test_an_exception_cannot_take_down_the_server_thread(self, monkeypatch, caplog):
        self._fake_platform(monkeypatch, supports_hotkey=True)
        boom: list[int] = []

        def _explode(url, watch=None):
            boom.append(1)
            raise RuntimeError("pid file on fire")

        monkeypatch.setattr("magent.launch.ensure_hotkey_listener", _explode)
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: len(boom) >= 2)

        with caplog.at_level("ERROR", logger="magent.hotkey"):
            self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert len(boom) == 2
        assert "listener check failed" in caplog.text

    def test_a_second_server_supervising_backs_off_instead_of_racing(self, monkeypatch):
        # Two serve daemons on different ports would otherwise both decide the
        # listener is missing and both spawn one.
        mod = self._mod()
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)

        def _held(name):
            raise mod.LockHeld(name)

        monkeypatch.setattr(mod, "exclusive_lock", _held)
        stop = threading.Event()
        seen: list[int] = []

        def _wait(timeout):
            seen.append(1)
            return True

        monkeypatch.setattr(stop, "wait", _wait)

        mod._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert calls == []  # never spawned behind the other supervisor's back
        assert seen == [1]  # and still slept rather than spinning


class TestDestFor:
    """Where one uploaded file lands, and what it is never allowed to clobber."""

    def test_a_file_from_an_earlier_request_is_never_overwritten(self, tmp_path):
        from magent.upload_server import _dest_for

        root = tmp_path.resolve()
        earlier = root / "1790000000_shot.png"
        earlier.write_bytes(b"earlier upload")
        dest = _dest_for(root, 1790000000, "shot.png")
        assert dest is not None
        assert dest != earlier
        assert dest.suffix == ".png"
        assert earlier.read_bytes() == b"earlier upload"

    def test_a_clash_keeps_the_original_extension(self, tmp_path):
        from magent.upload_server import _dest_for

        root = tmp_path.resolve()
        first = _dest_for(root, 1790000000, "notes.txt")
        assert first is not None
        second = _dest_for(root, 1790000000, "notes.txt")
        assert second is not None
        assert second != first
        assert second.suffix == ".txt"

    @pytest.mark.parametrize("name", ["..", "...", "."])
    def test_a_dots_only_name_becomes_upload(self, tmp_path, name):
        from magent.upload_server import _dest_for

        dest = _dest_for(tmp_path.resolve(), 1790000000, name)
        assert dest is not None
        assert dest.name == "1790000000_upload"

    def test_a_long_original_name_still_lands(self, tmp_path):
        # 250 chars is a legal name on every OS; the `<stamp>_` prefix used to
        # push it past one path component's 255 limit and the upload was a 500.
        from magent.upload_server import _dest_for

        dest = _dest_for(tmp_path.resolve(), 1790000000, "a" * 250 + ".txt")
        assert dest is not None
        dest.write_bytes(b"x")
        assert dest.suffix == ".txt"
        assert len(dest.name.encode()) <= 255
        assert dest.name.startswith("1790000000_aaaa")

    def test_a_long_non_ascii_name_is_cut_on_a_character(self, tmp_path):
        # Linux counts BYTES, so a non-ASCII name hits the limit sooner -- and
        # a cut through the middle of a character would be a mangled name.
        from magent.upload_server import _dest_for

        dest = _dest_for(
            tmp_path.resolve(),
            1790000000,
            "\N{LATIN SMALL LETTER E WITH ACUTE}" * 200 + ".png",
        )
        assert dest is not None
        assert dest.suffix == ".png"
        assert len(dest.name.encode()) <= 255
        assert set(dest.stem.removeprefix("1790000000_")) == {
            "\N{LATIN SMALL LETTER E WITH ACUTE}"
        }

    def test_a_long_suffix_is_trimmed_like_a_stem(self, tmp_path):
        from magent.upload_server import _dest_for

        dest = _dest_for(tmp_path.resolve(), 1790000000, "notes." + "x" * 300)
        assert dest is not None
        assert len(dest.name.encode()) <= 255

    def test_a_same_second_upload_in_another_request_never_shares_a_name(
        self, tmp_path
    ):
        # Two requests in one second, each choosing its dest before the other
        # has written: `exists()` could not see a name chosen but unwritten.
        from magent.upload_server import _dest_for

        root = tmp_path.resolve()
        first = _dest_for(root, 1790000000, "shot.png")
        second = _dest_for(root, 1790000000, "shot.png")
        assert first is not None and second is not None
        assert first != second
        assert first.exists() and second.exists()  # both are reserved on disk

    def test_concurrent_reservations_never_collide(self, tmp_path):
        from magent.upload_server import _dest_for

        root = tmp_path.resolve()
        got: list[Path | None] = []
        lock = threading.Lock()
        gate = threading.Barrier(8)

        def _reserve():
            gate.wait()
            dest = _dest_for(root, 1790000000, "shot.png")
            with lock:
                got.append(dest)

        threads = [threading.Thread(target=_reserve) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(got) == 8
        assert len(set(got)) == 8

    @pytest.mark.parametrize("name", [".env", ".gitignore", ".bashrc"])
    def test_a_dotfile_keeps_its_name(self, tmp_path, name):
        # With any-file uploads a dotfile is a real case; the `<stamp>_`
        # prefix already stops it being hidden, so it keeps what it is called.
        from magent.upload_server import _dest_for

        dest = _dest_for(tmp_path.resolve(), 1790000000, name)
        assert dest is not None
        assert dest.name == f"1790000000_{name}"

    def test_a_trailing_dot_is_dropped_so_the_path_names_the_file(self, tmp_path):
        # Windows silently drops a trailing dot on create; the returned (and
        # pasted) path has to be the file that actually exists.
        from magent.upload_server import _dest_for

        dest = _dest_for(tmp_path.resolve(), 1790000000, "notes.")
        assert dest is not None
        assert dest.name == "1790000000_notes"


class TestUploadedWhat:
    @pytest.mark.parametrize(
        "suffix", [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".JPG"]
    )
    def test_every_image_suffix_says_image(self, suffix):
        from magent.upload_server import _uploaded_what

        assert _uploaded_what(1, suffix) == "image"

    @pytest.mark.parametrize("suffix", [".svg", ".zip", ""])
    def test_anything_else_says_file(self, suffix):
        from magent.upload_server import _uploaded_what

        assert _uploaded_what(1, suffix) == "file"


class TestACloudPaneTakesNoUpload:
    """A cloud pane is a LOCAL viewer of a session that runs in a VM, which
    cannot read ``~/.magent/uploads`` on this PC (spec section 18.7): the upload
    is refused before a byte is written, with a flag Alt+V narrates by name."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.psmux as psmux_mod
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: None)
        self.upload_dir = tmp_path / "uploads"
        self.flashes: list[tuple] = []
        monkeypatch.setattr(mod, "_flash", lambda *a, **k: self.flashes.append(a))
        monkeypatch.setattr(UploadHandler, "config_path", None)
        self._sessions(
            monkeypatch,
            [
                {"name": "api", "session": "api", "node": "cloud"},
                {"name": "web", "session": "web", "node": None},
            ],
        )

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        yield
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _sessions(monkeypatch, rows: list[dict[str, object]]) -> None:
        monkeypatch.setattr(UploadHandler, "cached_sessions", rows)
        monkeypatch.setattr(UploadHandler, "sessions_ts", time.time() + 9999)

    def _post(self, project: str, *, flagged: bool = False) -> tuple[int, dict]:
        fields = (
            b""
            if flagged
            else (
                b"------B\r\n"
                b'Content-Disposition: form-data; name="project"\r\n\r\n'
                + project.encode()
                + b"\r\n"
            )
        )
        body = (
            fields + b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n0\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="shot.png"\r\n'
            b"Content-Type: image/png\r\n\r\nFAKEPNG\r\n"
            b"------B--\r\n"
        )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        path = f"/upload?project={project}" if flagged else "/upload"
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def _nothing_written(self) -> bool:
        return not (self.upload_dir.exists() and any(self.upload_dir.iterdir()))

    def _page(self) -> str:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        return conn.getresponse().read().decode()

    def _settled(self) -> None:
        """Wait until the POST handler has FINISHED, `finally` included.

        The status-line flash is issued after the reply is written, so a test
        that reads `self.flashes` straight after `_post` races it. This server
        is single-threaded: a second request is only answered once the first
        handler has returned."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health")
        conn.getresponse().read()

    def test_an_upload_to_a_cloud_pane_is_409_and_writes_nothing(self):
        status, body = self._post("api")
        assert status == 409
        assert body == {
            "ok": False,
            "cloud": True,
            "error": "cloud session: attach images at claude.ai/code or in the Claude app",
        }
        assert self._nothing_written()

    def test_the_alt_v_listeners_flagged_upload_is_refused_the_same_way(self):
        status, body = self._post("api", flagged=True)
        assert status == 409 and body["cloud"] is True
        assert self._nothing_written()

    def test_a_refused_cloud_upload_does_not_flash_the_panes_status_line(self):
        # The mobile page already shows the error text; "upload failed" on the
        # pane's own bar would call a deliberate refusal a fault.
        self._post("api")
        self._settled()
        assert self.flashes == []

    def test_an_ordinary_session_still_takes_the_upload(self):
        status, body = self._post("web")
        assert status == 200 and body["ok"] is True
        assert "cloud" not in body
        assert not self._nothing_written()
        # The control for the no-flash pin above: this harness DOES see the
        # confirmation an ordinary mobile upload gets.
        self._settled()
        assert len(self.flashes) == 1

    def test_an_unknown_project_is_still_the_plain_400(self):
        # The cloud 409 sits AFTER the Unknown-project check on purpose: only a
        # name the server knows can be a cloud pane.
        status, body = self._post("nope")
        assert status == 400 and "cloud" not in body

    def test_the_phone_page_does_not_offer_a_cloud_pane(self):
        page = self._page()
        assert 'data-name="web"' in page
        assert 'data-name="api"' not in page

    def test_the_page_shows_the_servers_own_error_text_whatever_the_status(self):
        # The 409's `error` is the only explanation a stale page (pills
        # rendered before the config changed) will ever show. Both send paths
        # must read the JSON body of a non-2xx and surface it.
        page = self._page()
        assert "pickFail(d.error" in page
        assert "pasteFail(d.error" in page

    def test_a_local_agent_that_shares_the_session_name_still_takes_the_upload(
        self, monkeypatch
    ):
        # `[local, cloud]` for one folder: the FIRST project owns the name
        # (psmux.cloud_pane_ids), so the pane is a local agent's and refusing
        # it would take a perfectly drivable pane away. One pill, not two.
        self._sessions(
            monkeypatch,
            [
                {"name": "api", "session": "api", "node": None},
                {"name": "api", "session": "api", "node": "cloud"},
            ],
        )
        status, body = self._post("api")
        assert status == 200 and body["ok"] is True
        assert self._page().count('data-name="api"') == 1

    def test_a_cloud_pane_that_shadows_a_local_twin_refuses_the_name(self, monkeypatch):
        # `[cloud, local]`: the cloud project owns the pane, so the shared name
        # is a cloud pane whichever row the page or the POST looks at.
        self._sessions(
            monkeypatch,
            [
                {"name": "api", "session": "api", "node": "cloud"},
                {"name": "api", "session": "api", "node": None},
            ],
        )
        status, body = self._post("api")
        assert status == 409 and body["cloud"] is True
        assert self._nothing_written()
        assert 'data-name="api"' not in self._page()
