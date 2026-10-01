"""What the published artifacts carry: nothing private.

PyPI is a second publication channel next to git. A file name on PyPI can never
be re-uploaded and mirrors copy an upload within minutes, so a private string in
an sdist or wheel cannot be scrubbed afterwards the way a branch can be
force-pushed. hatchling's default sdist ships the whole tree minus gitignored
files -- tests, docs, agent plans -- which is how a maintainer's checkout path
and account name reached earlier releases.

This tier builds the real sdist and scans every member of it AND of the wheel
the ``packaged`` fixture built. It can only look for kinds that are private on
ANY machine (a scratch-dir name, the cloud-sync/parent-folder shape of a real
checkout, a non-synthetic ``C:\\Users\\<name>`` profile path, the maintainer's
fleet-host naming scheme, a private or tailnet address outside the synthetic
blocks the tests use); a per-machine value such as the account name is scanned
by the release checklist instead, because a committed test must not carry the
string it forbids. The one real host name that is not covered by a shape is
denied by its SHA-256, so the denylist does not itself publish it.

The markers are assembled from fragments: this file ships in the sdist too, and
a contiguous literal would flag itself.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.dist.conftest import Packaged

pytestmark = pytest.mark.dist

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Kinds that are private wherever they appear. Fragments, never a literal.
_MARKERS: dict[str, re.Pattern[bytes]] = {
    "agent scratch dir": re.compile(b"mg" + b"tmp", re.IGNORECASE),
    "cloud-synced checkout folder": re.compile(b"one" + b"drive", re.IGNORECASE),
    "private parent folder": re.compile(b"custom[ _-]?mc" + b"ps", re.IGNORECASE),
    # Every fleet node is ``<prefix>-<name>``; tests and docs use ``node-<nick>``.
    "fleet host name": re.compile(b"devi" + b"no-", re.IGNORECASE),
    # The fleet's front machine is the bare prefix as a login target.
    "fleet login target": re.compile(b"@devi" + rb"no(?![\w.-])", re.IGNORECASE),
}
# Agent working plans are a PATH (pyproject's own exclude line names the
# directory, so a content match would flag the config that removes it).
_PLAN_DIR = "docs/super" + "powers"

# ``C:\Users\<name>`` (also escaped, forward-slashed, dash-encoded) where <name>
# is not one of the synthetic placeholders the tests use. Adding a new
# placeholder is a one-word change here; a real account name never belongs.
_SYNTHETIC_USERS = frozenset(
    {
        "a",
        "alice",
        "alice2",
        "api",
        "bob",
        "default",
        "demo",
        "foo",
        "me",
        "name",
        "public",
        "r",
        "someone",
        "user",
        "x",
        "you",
    }
)
_PROFILE_PATH = re.compile(
    rb"[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9_.-]+)", re.IGNORECASE
)

# Tailscale hands out 100.64.0.0/10. The tests use this /20 of it, and no real
# node address lies inside it. Any OTHER 100.x address is refused.
_SYNTHETIC_TAILNET = ipaddress.ip_network("100.64.0.0/20")
# Private / link-local addresses a test may use as a placeholder. A real LAN or
# tailnet address is a location; these blocks are the documentation and unit-test
# conventions, not anyone's network.
_SYNTHETIC_PRIVATE = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "169.254.0.0/16", "192.0.2.0/24", "192.168.1.0/24")
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# 0.0.0.0/8 is no host at all (a wildcard bind, or a 4-part version such as a Windows
# capability name), so it is never a location.
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_DOTTED_QUAD = re.compile(rb"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)")

# SHA-256 of real host names whose SHAPE is not matched above. Hashes, so the
# denylist does not publish what it forbids. Real IP addresses are deliberately
# NOT hashed (a 32-bit space: the hash would be reversible in seconds); they are
# covered by the address rules.
_DENIED_NAME_SHA256 = frozenset(
    {"e24e6ac3846e266191206c4b385e1b8dbb2decb7831f03a388f2c5c673b1e7b0"}
)
_NAME_TOKEN = re.compile(rb"[A-Za-z0-9][A-Za-z0-9._-]*")


def _foreign_address(data: bytes) -> bool:
    """True if ``data`` holds a private/tailnet IPv4 outside the synthetic blocks."""
    for m in _DOTTED_QUAD.finditer(data):
        try:
            addr = ipaddress.ip_address(m.group(1).decode())
        except ValueError:
            continue
        if addr.is_loopback or addr in _THIS_NETWORK:
            continue
        if addr in _SYNTHETIC_TAILNET or any(addr in n for n in _SYNTHETIC_PRIVATE):
            continue
        if addr in _CGNAT or addr.is_private or addr.packed[0] == 100:
            return True
    return False


def _denied_name(data: bytes) -> bool:
    for tok in set(_NAME_TOKEN.findall(data)):
        if (
            hashlib.sha256(tok.rstrip(b"._-").lower()).hexdigest()
            in _DENIED_NAME_SHA256
        ):
            return True
    return False


def _members(archive: Path):
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not name.endswith("/"):
                    yield name, z.read(name)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if m.isfile():
                    f = t.extractfile(m)
                    yield m.name, f.read() if f else b""


def _member_findings(name: str, data: bytes) -> list[str]:
    out: list[str] = []
    if _PLAN_DIR in name:
        out.append(f"agent-plan directory: {name}")
    for kind, rx in _MARKERS.items():
        if rx.search(data) or rx.search(name.encode()):
            out.append(f"{kind}: {name}")
    for m in _PROFILE_PATH.finditer(data):
        if m.group(1).decode().lower() not in _SYNTHETIC_USERS:
            out.append(f"non-synthetic Windows profile path: {name}")
            break
    if _foreign_address(data):
        out.append(f"private or tailnet address outside the synthetic blocks: {name}")
    if _denied_name(data) or _denied_name(name.encode()):
        out.append(f"denied real host name: {name}")
    return out


def _findings(archive: Path) -> list[str]:
    """Kind + member only. The matched text is never echoed (it is the leak)."""
    out: list[str] = []
    for name, data in _members(archive):
        out.extend(_member_findings(name, data))
    return sorted(set(out))


@pytest.fixture(scope="module")
def sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    outdir = tmp_path_factory.mktemp("sdisthouse")
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--outdir",
            str(outdir),
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, (
        f"sdist build failed:\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    found = sorted(outdir.glob("magent_multi_ai_agents_manager-*.tar.gz"))
    assert len(found) == 1, f"expected exactly one sdist, got {found}\n{build.stdout}"
    return found[0]


def test_the_sdist_carries_nothing_private(sdist: Path) -> None:
    assert _findings(sdist) == []


def test_the_wheel_carries_nothing_private(packaged: Packaged) -> None:
    assert _findings(packaged.wheel) == []


def test_the_sdist_still_builds_the_wheel_it_ships(sdist: Path, tmp_path: Path) -> None:
    """The release pipeline's ``python -m build`` makes the wheel FROM the sdist,
    so an over-eager exclude that drops a file the build needs (README for the
    metadata, LICENSE, src) only shows up there. Prove the round trip here."""
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(tmp_path),
            str(sdist),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, (
        f"wheel-from-sdist failed:\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert list(tmp_path.glob("magent_multi_ai_agents_manager-*.whl"))


class TestTheScannerActuallyCatchesEachKind:
    """A privacy scan that cannot fail proves nothing. Every rule is fed a
    positive here, built from fragments so this file does not flag itself."""

    @pytest.mark.parametrize(
        "text",
        [
            "ssh root@" + "devi" + "no-second",
            "host: " + "DEVI" + "NO-Fifth",
            "target=demo@" + "devi" + "no",
        ],
    )
    def test_a_fleet_host_name_is_refused(self, text: str) -> None:
        got = _member_findings("x.py", text.encode())
        assert any(g.startswith(("fleet host name", "fleet login target")) for g in got)

    @pytest.mark.parametrize(
        "addr",
        [
            "100." + "127.9.9",  # CGNAT, outside the synthetic /20
            "100." + "16.0.1",  # a 100.x that is not even CGNAT
            "192.168." + "77.5",  # a LAN address outside the placeholder /24
            "172.16." + "4.4",
        ],
    )
    def test_an_address_outside_the_synthetic_blocks_is_refused(
        self, addr: str
    ) -> None:
        assert _foreign_address(f"http://{addr}:8033/".encode())

    @pytest.mark.parametrize(
        "text",
        [
            "100.64.0.1",
            "100.64.15.254",  # the top of the synthetic /20
            "10.0.0.5",
            "192.168.1.20",
            "127.0.0.1",
            "0.0.0.0",
            "OpenSSH.Server~~~~0.0.1.0",
            "8.8.8.8",  # public: not a location of ours
            "version 1.100.2.3.4 is not an address",
        ],
    )
    def test_the_placeholders_the_tests_use_pass(self, text: str) -> None:
        assert not _foreign_address(text.encode())

    def test_a_hashed_name_is_refused_in_content_and_in_a_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = "not-a-real-host.invalid"
        monkeypatch.setattr(
            sys.modules[__name__],
            "_DENIED_NAME_SHA256",
            frozenset({hashlib.sha256(fake.encode()).hexdigest()}),
        )
        assert _denied_name(f"ssh {fake}. now".encode())
        assert not _denied_name(b"ssh some-other-host.invalid")
        assert _member_findings("a.txt", f"HostName {fake}".encode()) == [
            "denied real host name: a.txt"
        ]
        assert _member_findings(f"{fake}", b"x") == [f"denied real host name: {fake}"]

    def test_the_committed_denylist_holds_hashes_not_names(self) -> None:
        for digest in _DENIED_NAME_SHA256:
            assert re.fullmatch(r"[0-9a-f]{64}", digest)
