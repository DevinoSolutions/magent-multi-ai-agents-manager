import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.platform


@pytest.fixture
def platform():
    from magent.platform import get_platform

    return get_platform()


def _has_gui():
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        r = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to count processes'],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    return bool(os.environ.get("DISPLAY"))


class TestFindWindow:
    @pytest.fixture
    def notepad_window(self):
        if not _has_gui():
            pytest.skip("no GUI session available")
        if sys.platform == "win32":
            proc = subprocess.Popen(["notepad.exe"])
            time.sleep(1)
            yield "Untitled - Notepad"
            proc.kill()
        elif sys.platform == "darwin":
            try:
                r = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "TextEdit" to make new document',
                    ],
                    capture_output=True,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                pytest.skip("TextEdit timed out in headless CI")
            if r.returncode != 0:
                pytest.skip("TextEdit unavailable in headless CI")
            time.sleep(2)
            yield "Untitled"
            subprocess.run(
                ["osascript", "-e", 'tell application "TextEdit" to quit'], check=False
            )
        else:
            if not shutil.which("xterm"):
                pytest.skip("xterm not installed")
            proc = subprocess.Popen(
                [
                    "xterm",
                    "-T",
                    "test-magent-find",
                    "-fa",
                    "Monospace",
                    "-fs",
                    "10",
                    "-e",
                    "sleep 30",
                ],
                stderr=subprocess.DEVNULL,
            )
            time.sleep(3)
            yield "test-magent-find"
            proc.kill()

    def test_find_existing_window(self, platform, notepad_window):
        handle = platform.find_window(notepad_window, mode="contains")
        assert handle is not None

    def test_find_nonexistent_window(self, platform):
        handle = platform.find_window("__nonexistent_window_title_99999__")
        assert handle is None
