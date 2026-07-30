"""The drift gate must tell an HTTP 404 apart from a TLS stall.

``scripts/check-schemas-in-sync.sh`` is what keeps every vendored contract in
``molecule_runtime/contracts/`` (and ``contracts/``) a MIRROR of molecule-ai-sdk
rather than a fork. It used to fetch with ``curl -fsS``, and ``curl -f`` exits
non-zero for an HTTP 404 in exactly the same way it exits non-zero for a DNS
failure or a TLS stall. Both landed on the soft-skip arm, so:

    a mapped SDK path that is renamed, moved, or typo'd in MAP
      -> 404
      -> "could not fetch … skipping drift check"
      -> gate GREEN, forever, having compared nothing

which is precisely the state ``contracts/PROVENANCE.md`` says the map exists to
prevent — *"a vendored file absent from that map is a mirror nothing checks, which
is how a mirror silently becomes a fork"*. A mapped file whose fetch always 404s
is that same unchecked mirror, only harder to notice, because it has a map entry
and prints a reassuring warning.

These tests drive the REAL script against a REAL local HTTP server over REAL curl
— no monkeypatched fetch — because the thing under test IS curl's exit status vs
its ``%{http_code}``, and a fake fetch is exactly the kind of stand-in that would
have accepted the old behaviour.

Four arms, and the pair of them in opposite directions is the point:

  * every mapped path served    -> exit 0
  * ONE mapped path 404s        -> exit 1 (HARD FAIL, names the file)
  * the server is not listening -> exit 2 (soft skip — still true, still needed)
  * the server 500s             -> exit 2 (forge-side infra, not a map bug)
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GATE = _REPO_ROOT / "scripts" / "check-schemas-in-sync.sh"

# `  [local/rel/path]="sdk/remote/path"` inside the declare -A MAP block.
_MAP_ENTRY = re.compile(r'^\s*\[([^\]]+)\]="([^"]+)"\s*$')


def _parse_map() -> dict[str, str]:
    """Read MAP out of the script itself.

    Deliberately parsed rather than duplicated: a hard-coded copy here would drift
    from the gate, and a test whose fixture disagrees with the gate's own map is a
    test of nothing.
    """
    text = _GATE.read_text(encoding="utf-8")
    body = text.split("declare -A MAP=(", 1)[1].split("\n)", 1)[0]
    mapping = {m.group(1): m.group(2) for m in map(_MAP_ENTRY.match, body.splitlines()) if m}
    assert mapping, "could not parse MAP out of check-schemas-in-sync.sh"
    return mapping


class _Handler(http.server.BaseHTTPRequestHandler):
    docroot: Path
    force_500 = False

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        if self.force_500:
            self.send_error(500, "forge is unwell")
            return
        target = self.docroot / self.path.lstrip("/")
        try:
            payload = target.read_bytes()
        except OSError:
            # The realistic shape of a renamed/typo'd MAP entry: the forge answers,
            # and its answer is "not there".
            self.send_error(404, "no such contract")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):  # keep pytest output readable
        pass


def _serve(docroot: Path, *, force_500: bool = False):
    handler = type("_H", (_Handler,), {"docroot": docroot, "force_500": force_500})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _closed_port() -> int:
    """A port with nothing on it — curl gets ECONNREFUSED, i.e. a transport error
    with no HTTP status at all. The stall/DNS/TLS class, reproduced deterministically."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bash() -> str:
    """A bash >= 4 — the gate's MAP is an associative array.

    /bin/bash on macOS is still 3.2, where `declare -A` fails; the gate now refuses
    to run there rather than exiting 0 having checked nothing, but these tests want
    the real classifier, so they find a capable bash.
    """
    for candidate in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if not path:
            continue
        probe = subprocess.run(
            [path, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, text=True
        )
        if probe.stdout.strip().isdigit() and int(probe.stdout.strip()) >= 4:
            return path
    pytest.fail("no bash >= 4 on this machine; scripts/check-schemas-in-sync.sh cannot run")


def _run(base: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["SCHEMA_SYNC_SDK_BASE"] = base
    return subprocess.run(
        [_bash(), str(_GATE)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


@pytest.fixture(scope="module", autouse=True)
def _require_curl():
    if shutil.which("curl") is None:
        # NOT a skip. curl is a hard dependency of the gate itself (and of ci.yml's
        # vendored-channel-client step), so "no curl" means the gate cannot run at
        # all — an inert gate must not read as green.
        pytest.fail("curl is missing: scripts/check-schemas-in-sync.sh cannot run")


@pytest.fixture
def sdk_mirror(tmp_path) -> Path:
    """A docroot that serves every mapped SDK path with the vendored bytes, so the
    baseline is a genuine in-sync run and any red below is attributable."""
    docroot = tmp_path / "sdk"
    for local_rel, remote_path in _parse_map().items():
        dest = docroot / remote_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO_ROOT / local_rel, dest)
    return docroot


def test_all_paths_resolve_and_match_is_green(sdk_mirror):
    """Baseline. Without this the reds below could be the harness, not the gate."""
    srv = _serve(sdk_mirror)
    try:
        res = _run(f"http://127.0.0.1:{srv.server_port}")
    finally:
        srv.shutdown()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "All vendored schemas are in sync" in res.stdout


def test_a_404_on_one_mapped_path_hard_fails_the_gate(sdk_mirror):
    """THE finding. One mapped path stops resolving — the SDK renamed it, or the
    MAP entry was typo'd — and the gate must RED, not print a warning and pass.

    Under the old `curl -fsS` this exited 2 and the workflow turned that into a
    green soft-skip, so the vendored copy became an unwatched fork.
    """
    victim_local = "molecule_runtime/contracts/plugin-install-report.contract.json"
    victim_remote = _parse_map()[victim_local]
    (sdk_mirror / victim_remote).unlink()

    srv = _serve(sdk_mirror)
    try:
        res = _run(f"http://127.0.0.1:{srv.server_port}")
    finally:
        srv.shutdown()

    out = res.stdout + res.stderr
    assert res.returncode == 1, (
        f"a 404 must HARD FAIL (exit 1), got exit {res.returncode}. "
        f"exit 2 is the soft skip that hid this exact bug.\n{out}"
    )
    assert "HTTP 404" in out
    assert "does not resolve" in out
    assert victim_local in out, "the gate must name the mirror it could not check"
    # And it must not be mistaken for infra.
    assert "skipping drift check for " + victim_local not in out


def test_a_transport_error_still_soft_skips():
    """The other direction, and the reason the soft skip exists at all: a real
    transport failure (nothing listening — the DNS/TLS/reset class) teaches us
    nothing about the mirror, so the gate must warn and skip, NOT red. Tightening
    the 404 arm must not turn every git.* stall into a false red on every PR."""
    res = _run(f"http://127.0.0.1:{_closed_port()}")
    out = res.stdout + res.stderr
    assert res.returncode == 2, (
        f"a transport failure must soft-skip (exit 2), got exit {res.returncode}.\n{out}"
    )
    assert "could not reach molecule-ai-sdk" in out
    assert "curl exit" in out
    assert "::error::" not in out, "a transport stall must not be reported as drift"


def test_a_forge_5xx_still_soft_skips(sdk_mirror):
    """A 500 is the forge being unwell — infra, like the stall — not a statement
    that the path is gone. Soft skip, and distinctly worded so the log says which
    of the two happened."""
    srv = _serve(sdk_mirror, force_500=True)
    try:
        res = _run(f"http://127.0.0.1:{srv.server_port}")
    finally:
        srv.shutdown()
    out = res.stdout + res.stderr
    assert res.returncode == 2, f"got exit {res.returncode}\n{out}"
    assert "HTTP 500" in out
    assert "forge-side" in out
    assert "::error::" not in out


def test_an_incapable_bash_reds_instead_of_exiting_zero(sdk_mirror):
    """Found by writing the tests above: under bash 3.2 (still /bin/bash on macOS)
    `declare -A MAP` fails, the loop iterates nothing, and the script used to fall
    through to `exit 0` — a green run that compared not one byte. Same class as the
    404 soft-skip: a gate that checked nothing must never report success.

    Skipped where no bash < 4 exists (Linux CI); it is a real, reproducible red on
    any Mac, which is where the gate gets run by hand.
    """
    old = None
    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, text=True
        )
        if probe.stdout.strip().isdigit() and int(probe.stdout.strip()) < 4:
            old = candidate
            break
    if old is None:
        pytest.skip("no bash < 4 on this machine to exercise the guard")

    env = dict(os.environ)
    env["SCHEMA_SYNC_SDK_BASE"] = "http://127.0.0.1:1"
    res = subprocess.run(
        [old, str(_GATE)], capture_output=True, text=True, env=env, timeout=60
    )
    out = res.stdout + res.stderr
    assert res.returncode != 0, f"an unusable bash must not read as in sync\n{out}"
    assert "NOTHING was checked" in out


def test_every_vendored_file_in_the_map_exists_locally():
    """The map's other half. A MAP key pointing at a local file that is not there
    already hard-fails inside the gate; this states it as a standalone invariant so
    the failure names the map, not a mystery ::error:: in a CI log."""
    missing = [rel for rel in _parse_map() if not (_REPO_ROOT / rel).is_file()]
    assert not missing, f"MAP names vendored files that do not exist: {missing}"
