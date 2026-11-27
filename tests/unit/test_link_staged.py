"""POST /api/v1/uploads in its staged form: a file the link pulled, named by
path -- only from a linked caller, only from the link's incoming folder."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from magent import api, events, link_staged, linkfiles
from tests.unit._link_fakes import up_id

PORT = 8080


@pytest.fixture
def bus():
    return events.EventBus("deadbeef")


@pytest.fixture
def ctx(tmp_path, bus):
    return api.ApiContext(
        config_path=None,
        bus=bus,
        allowed_hosts=frozenset({"127.0.0.1"}),
        upload_dir=tmp_path / "uploads",
        upload_max_bytes=1000,
        request_limit=2000,
        health=lambda: api.Health("magent-upload", PORT, 4321, 5.0, 1, 0.5),
        upload_sessions=lambda: {"magent-alpha"},
        inject_fn=lambda _session, _text: (True, False),
    )


@pytest.fixture(autouse=True)
def _psmux(monkeypatch):
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")


def _staged_file(name: str = "photo.png", data: bytes = b"PNG") -> Path:
    folder = linkfiles.INCOMING_DIR / up_id(1)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def _post(
    ctx, body: dict[str, object], kind: str = "linked", ctype: str | None = None
) -> api.ApiResponse:
    raw = json.dumps(body).encode()
    req = api.ApiRequest(
        method="POST",
        path="/api/v1/uploads",
        query={},
        headers={
            "x-magent-client": "magent-relay",
            "content-type": ctype or link_staged.CONTENT_TYPE,
        },
        body=api.Body.of(raw),
        peer="link",
        bind="link",
        port=0,
    )
    answer = api.handle(req, ctx, api.Caller(kind))
    assert isinstance(answer, api.ApiResponse)
    return answer


def _body(path: Path, **over: object) -> dict[str, object]:
    return {
        "staged_path": str(path),
        "name": path.name,
        "session": "magent-alpha",
        "inject": True,
        **over,
    }


def _reason(answer: api.ApiResponse) -> object:
    error = answer.body["error"]
    assert isinstance(error, dict)
    return error.get("details", {}).get("reason")


def test_the_content_type_is_recognised():
    assert link_staged.is_staged("application/x-magent-staged+json; charset=utf-8")
    assert link_staged.is_staged("Application/X-Magent-Staged+JSON")
    assert not link_staged.is_staged("multipart/form-data; boundary=x")
    assert not link_staged.is_staged("application/json")
    assert not link_staged.is_staged(None)


def test_a_linked_caller_saves_pastes_and_announces(ctx, bus):
    answer = _post(ctx, _body(_staged_file()))
    assert answer.status == 200, answer.body
    data = answer.body["data"]
    assert isinstance(data, dict)
    assert data["paste"] == "injected"
    assert Path(str(data["path"])).read_bytes() == b"PNG"
    [event] = bus.since("deadbeef:0").events
    assert (event.type, event.data["session"]) == ("upload", "magent-alpha")


def test_no_paste_asked_is_saved_only(ctx):
    answer = _post(ctx, _body(_staged_file(), inject=False))
    data = answer.body["data"]
    assert isinstance(data, dict)
    assert data["paste"] == "saved"


def test_the_saved_file_takes_the_name_given(ctx):
    answer = _post(ctx, _body(_staged_file("blob.bin"), name="holiday.png"))
    data = answer.body["data"]
    assert isinstance(data, dict)
    assert Path(str(data["path"])).name.endswith("holiday.png")


@pytest.mark.parametrize("kind", ["loopback", "remote"])
def test_only_a_linked_caller_may_use_the_staged_form(ctx, kind):
    answer = _post(ctx, _body(_staged_file()), kind=kind)
    assert (answer.status, _reason(answer)) == (403, "not_linked")


def test_a_linked_caller_still_has_the_multipart_form(ctx):
    # The staged form is chosen by its Content-Type alone: a JSON body sent
    # as anything else never reaches it.
    answer = _post(ctx, _body(_staged_file()), ctype="application/json")
    assert answer.status == 400


def test_a_file_outside_the_incoming_folder_is_refused(ctx, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("DECOY secret", encoding="utf-8")
    answer = _post(ctx, _body(secret))
    assert (answer.status, _reason(answer)) == (403, "outside_incoming")


def test_climbing_out_of_the_incoming_folder_is_refused(ctx):
    # ~/.magent/uploads/.incoming/<id>/../../../secret.txt is ~/.magent's.
    folder = _staged_file().parent
    secret = linkfiles.INCOMING_DIR.parent.parent / "secret.txt"
    secret.write_text("DECOY secret", encoding="utf-8")
    climb = folder / ".." / ".." / ".." / "secret.txt"
    assert climb.resolve() == secret.resolve()
    answer = _post(ctx, _body(climb, name="secret.txt"))
    assert (answer.status, _reason(answer)) == (403, "outside_incoming")


def test_a_link_inside_the_incoming_folder_is_refused(ctx, tmp_path):
    # Resolved, a link (a junction on Windows) that sits inside the folder
    # but points out names a file outside it.
    folder = _staged_file().parent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("DECOY secret", encoding="utf-8")
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(folder / "j"))
    else:
        os.symlink(outside, folder / "j")
    answer = _post(ctx, _body(folder / "j" / "secret.txt", name="secret.txt"))
    assert (answer.status, _reason(answer)) == (403, "outside_incoming")


def test_the_incoming_folder_itself_is_refused(ctx):
    linkfiles.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    answer = _post(ctx, _body(linkfiles.INCOMING_DIR))
    assert (answer.status, _reason(answer)) == (403, "outside_incoming")


def test_a_relative_path_is_refused(ctx):
    answer = _post(ctx, _body(Path("photo.png")))
    assert (answer.status, _reason(answer)) == (403, "outside_incoming")


def test_a_relative_path_is_refused_even_from_inside_the_folder(ctx, monkeypatch):
    # What a relative path names depends on the daemon's working folder:
    # refused whatever it would resolve to.
    monkeypatch.chdir(_staged_file().parent)
    answer = _post(ctx, _body(Path("photo.png")))
    assert (answer.status, _reason(answer)) == (403, "outside_incoming")


def test_a_missing_staged_file_is_404(ctx):
    linkfiles.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    answer = _post(ctx, _body(linkfiles.INCOMING_DIR / "gone.png"))
    assert (answer.status, _reason(answer)) == (404, "staged_missing")


def test_a_folder_is_not_a_staged_file(ctx):
    folder = _staged_file().parent
    answer = _post(ctx, _body(folder))
    assert (answer.status, _reason(answer)) == (404, "staged_missing")


def test_an_unknown_session_is_404(ctx):
    answer = _post(ctx, _body(_staged_file(), session="ghost"))
    assert (answer.status, _reason(answer)) == (404, "unknown_session")


def test_a_file_over_the_cap_is_413(ctx):
    answer = _post(ctx, _body(_staged_file(data=b"x" * 1001)))
    assert answer.status == 413


@pytest.mark.parametrize(
    "over",
    [
        {"staged_path": None},
        {"name": ""},
        {"session": 7},
        {"inject": "yes"},
    ],
)
def test_a_malformed_body_is_400(ctx, over):
    answer = _post(ctx, _body(_staged_file(), **over))
    assert answer.status == 400
