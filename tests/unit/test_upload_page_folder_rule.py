"""The upload page's folder decision: what every surface does with its answer."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from magent.upload_server import _build_html


def _html() -> str:
    return _build_html([{"name": "p", "path": "x"}])


def _block(html: str, start: str, end: str = "\n});") -> str:
    return html.split(start, 1)[1].split(end, 1)[0]


class TestTheFolderDecisionIsWiredToEverySurface:
    """Drift pins for the halves of the folder rule the existing pins leave
    open: what each surface DOES with itemIsFolder's answer."""

    def test_one_folder_in_a_drop_refuses_the_whole_drop(self):
        # `.some`, not `.every`: one folder in a mixed drop refuses it all,
        # and the drop asks the entry-aware function, never the bare guess.
        drop = _block(_html(), "input.addEventListener('drop'")
        assert ".some(it => it.kind === 'file' && itemIsFolder(it))" in drop
        assert "looksLikeFolder" not in drop

    def test_a_pasted_folder_refuses_the_paste_instead_of_vanishing(self):
        # Skipping the folder item would stage the rest of a mixed paste --
        # the "sent something the user did not pick" case the rule exists for.
        paste = _block(_html(), "window.addEventListener('paste'")
        assert "if (itemIsFolder(it)) { folder = true; continue; }" in paste
        assert "if (!files.length && !folder) return;" in paste
        assert paste.index("if (folder) { refuse(FOLDER); return; }") < paste.index(
            "stageFiles(files)"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_item_is_folder_answers_like_the_rule_under_real_node(tmp_path):
    # Behavioural, not textual: the SERVED functions, run by a real JS engine
    # against item shapes a browser hands the page.
    html = _html()
    start = html.index("function looksLikeFolder(")
    end = html.index("\n}", html.index("function itemIsFolder(")) + 2
    cases = """
const file = (size, type) => () => ({size, type});
const entry = isDirectory => () => ({isDirectory});
const cases = [
  ["entry says folder (empty File)", {getAsFile: file(0, ''), webkitGetAsEntry: entry(true)}, true],
  ["entry says folder (sized File)", {getAsFile: file(4096, ''), webkitGetAsEntry: entry(true)}, true],
  ["entry says file: empty .toml", {getAsFile: file(0, ''), webkitGetAsEntry: entry(false)}, false],
  ["no entry API: empty typeless", {getAsFile: file(0, '')}, true],
  ["no entry API: real file", {getAsFile: file(5, 'text/plain')}, false],
  ["null entry: empty typeless", {getAsFile: file(0, ''), webkitGetAsEntry: () => null}, true],
  ["entry throws: empty typeless", {getAsFile: file(0, ''), webkitGetAsEntry: () => { throw new Error('x'); }}, true],
  ["entry throws: real file", {getAsFile: file(5, 'image/png'), webkitGetAsEntry: () => { throw new Error('x'); }}, false],
  ["no File at all", {getAsFile: () => null}, false],
];
process.stdout.write(JSON.stringify(cases.map(([n, it, want]) => [n, itemIsFolder(it), want])));
"""
    script = tmp_path / "folder.js"
    script.write_text(html[start:end] + "\n" + cases, encoding="utf-8")
    r = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert r.returncode == 0, r.stderr
    wrong = [(n, got, want) for n, got, want in json.loads(r.stdout) if got is not want]
    assert not wrong, f"itemIsFolder decided wrongly: {wrong}"
