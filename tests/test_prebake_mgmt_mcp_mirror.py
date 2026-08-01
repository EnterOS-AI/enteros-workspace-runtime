"""#393 — the image prebake must resolve the management-MCP tree from OUR npm
mirror only, from a PINNED lock, and fail LOUDLY on a mirror miss.

Before this, ``prebake-mgmt-mcp.sh`` pointed only the ``@molecule-ai`` SCOPE at
our registry and then ran ``npm install @molecule-ai/mcp-server@<ver>``. Three
consequences, all reproduced as failing assertions here:

1. the TRANSITIVE tree (@modelcontextprotocol/sdk, pino, zod, express, ...) came
   live from registry.npmjs.org, so an npmjs.org outage blocked producing a
   workspace image at all (it took CI down on 2026-08-01);
2. every transitive semver RANGE was re-resolved at build time, so two builds of
   the same template commit were not guaranteed to contain the same MCP server;
3. nothing asserted the absence of an upstream fallback, so the
   dependency-confusion surface we closed for PyPI stayed open on the npm side.

The tests split into two layers:

* PIN tests read the vendored lock as data — cheap, run everywhere.
* BEHAVIOUR tests EXECUTE the real shell script against stubbed ``npm``/``npx``
  binaries that record every argv+env. Asserting on the recorded invocations is
  what makes "resolves from the mirror" and "fails loudly" falsifiable rather
  than a grep over comments that any reword would satisfy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from molecule_runtime import platform_agent_identity as pai

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PKG = REPO_ROOT / "molecule_runtime"
LOCK_DIR = RUNTIME_PKG / pai.MANAGEMENT_MCP_LOCK_DIR
PREBAKE = RUNTIME_PKG / "scripts" / "prebake-mgmt-mcp.sh"
UPSTREAM = pai.MANAGEMENT_MCP_UPSTREAM_REGISTRY_HOST


# --------------------------------------------------------------------------
# the vendored pin
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads((LOCK_DIR / "package-lock.json").read_text())


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((LOCK_DIR / "package.json").read_text())


def test_vendored_pin_exists():
    assert (LOCK_DIR / "package.json").is_file()
    assert (LOCK_DIR / "package-lock.json").is_file()


def test_pin_ships_in_the_wheel():
    # A wheel without the lock cannot bake at all. package-data is the only
    # mechanism that puts it there.
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    globs = data["tool"]["setuptools"]["package-data"]["molecule_runtime"]
    assert f"{pai.MANAGEMENT_MCP_LOCK_DIR}/*.json" in globs


def test_manifest_pins_the_exact_ssot_version(manifest):
    # EXACT, not a range: this file is the reproducibility pin. `^1.9.6` would
    # let two builds of the same commit bake different servers.
    pinned = manifest["dependencies"][pai.MANAGEMENT_MCP_NPM_PACKAGE]
    assert pinned == pai.MANAGEMENT_MCP_PINNED_VERSION
    assert pinned.strip()[0].isdigit(), f"range operator in pin: {pinned!r}"


def test_lock_root_matches_the_manifest(lock, manifest):
    assert lock["packages"][""]["dependencies"] == manifest["dependencies"]


def test_lock_is_v3_or_newer(lock):
    # v1 locks carry no `packages` map and no per-entry integrity.
    assert lock["lockfileVersion"] >= 3


def test_lock_contains_the_pinned_management_mcp(lock):
    entry = lock["packages"][f"node_modules/{pai.MANAGEMENT_MCP_NPM_PACKAGE}"]
    assert entry["version"] == pai.MANAGEMENT_MCP_PINNED_VERSION


def test_pinned_version_satisfies_the_launch_range(lock):
    # Guard D stays true through the pin: what we bake must satisfy what the
    # concierge launches.
    major = pai.MANAGEMENT_MCP_PINNED_VERSION.split(".")[0]
    assert pai.MANAGEMENT_MCP_COMPATIBLE_RANGE.lstrip("^~").split(".")[0] == major


def test_every_lock_entry_resolves_to_our_mirror(lock):
    """THE assertion. A lock entry's ``resolved`` URL OVERRIDES the configured
    registry, so one upstream URL left here reopens the live npmjs.org
    dependency no matter what the .npmrc says."""
    offenders = [
        (path, entry["resolved"])
        for path, entry in lock["packages"].items()
        if entry.get("resolved")
        and not entry["resolved"].startswith(pai.MANAGEMENT_MCP_REGISTRY)
    ]
    assert offenders == [], (
        f"{len(offenders)} entries resolve off-mirror: {offenders[:3]}"
    )


def test_lock_never_mentions_the_upstream_registry(lock):
    assert UPSTREAM not in json.dumps(lock)


def test_every_lock_entry_is_integrity_pinned(lock):
    # Serving from our own host means WE could serve different bytes; the
    # sha512 is what keeps the mirror honest.
    missing = [
        path
        for path, entry in lock["packages"].items()
        if path and not entry.get("integrity")
    ]
    assert missing == []


def test_lock_pins_the_transitive_tree_not_just_the_top_level(lock):
    # The 2026-08-01 outage was on a TRANSITIVE dep (@modelcontextprotocol/sdk),
    # and that dep floats on a `^1.12.0` range upstream. The pin is worthless if
    # it only covers the top-level package.
    entries = {p for p in lock["packages"] if p}
    assert len(entries) > 50, entries
    sdk = lock["packages"]["node_modules/@modelcontextprotocol/sdk"]
    assert sdk["version"][0].isdigit() and sdk["resolved"].startswith(
        pai.MANAGEMENT_MCP_REGISTRY
    )


# --------------------------------------------------------------------------
# the prebake script, executed against stubbed npm/npx
# --------------------------------------------------------------------------

_STUB = """#!/bin/sh
# Record argv + the npm_config_* env this invocation saw, then emulate success.
{
  printf '%s' "$(basename "$0")"
  for a in "$@"; do printf '\\t%s' "$a"; done
  printf '\\tENV_registry=%s\\tENV_cache=%s\\n' "${npm_config_registry:-}" "${npm_config_cache:-}"
} >> "$MOLECULE_STUB_LOG"
"""

# npm: `ci` may be told to fail (mirror-miss simulation); everything else is a
# no-op success. npx: emits a tools/list containing the required tool unless the
# test asks it to emit nothing.
_NPM_STUB = (
    _STUB
    + """
case "$1" in
  ci) [ -n "${MOLECULE_STUB_NPM_CI_FAILS:-}" ] && { echo "npm error 404 Not Found - GET ${npm_config_registry}not-mirrored" >&2; exit 1; } ;;
esac
exit 0
"""
)

_NPX_STUB = (
    _STUB
    + """
[ -n "${MOLECULE_STUB_NPX_SILENT:-}" ] && exit 0
cat <<'JSON'
{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"__TOOL__"}]}}
JSON
exit 0
"""
)


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def harness(tmp_path: Path):
    """A fake node toolchain + agent HOME; returns a runner for the real script."""
    # PROBE the capability, do not infer it from a PATH hit: on Windows
    # `shutil.which("bash")` finds the WSL shim, which execs nothing.
    if not _bash_works():  # pragma: no cover - platform guard
        pytest.skip("a working bash is required to execute the prebake script")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    _write_stub(bindir / "npm", _NPM_STUB)
    _write_stub(bindir / "npx", _NPX_STUB.replace("__TOOL__", pai.REQUIRED_TOOL))
    # `node` is used for two real jobs (read the pin, expand the lock into cache
    # specs); a stub would make those vacuous, so require the real binary.
    if shutil.which("node") is None:  # pragma: no cover - platform guard
        pytest.skip("node unavailable")

    home = tmp_path / "home"
    home.mkdir()

    def run(
        *, lock_files: dict[str, str] | None = None, env: dict[str, str] | None = None
    ):
        real_node = Path(shutil.which("node")).parent
        pythonpath = str(REPO_ROOT)
        if lock_files is not None:
            # Swap the vendored pin WITHOUT a test-only hook in the production
            # script: a shim `molecule_runtime` package whose __path__ extends
            # to the real one. The script derives the lock dir from
            # `molecule_runtime.__file__`, so it reads the shim's copy while
            # every real symbol still imports from the real package.
            shim = tmp_path / f"shim-{len(list(tmp_path.glob('shim-*')))}"
            pkg = shim / "molecule_runtime"
            (pkg / pai.MANAGEMENT_MCP_LOCK_DIR).mkdir(parents=True)
            (pkg / "__init__.py").write_text(f"__path__.append({str(RUNTIME_PKG)!r})\n")
            for name, body in lock_files.items():
                (pkg / pai.MANAGEMENT_MCP_LOCK_DIR / name).write_text(body)
            pythonpath = os.pathsep.join([str(shim), pythonpath])
        environ = {
            **os.environ,
            "HOME": str(home),
            "PATH": os.pathsep.join(
                [str(bindir), str(real_node), os.environ.get("PATH", "")]
            ),
            "MOLECULE_STUB_LOG": str(log),
            "MOLECULE_RUNTIME_PYTHON": _python(),
            "PYTHONPATH": pythonpath,
        }
        environ.pop("MOLECULE_PREBAKE_NODE_BIN", None)
        environ.update(env or {})
        return subprocess.run(
            ["bash", str(PREBAKE)],
            capture_output=True,
            text=True,
            env=environ,
            cwd=str(tmp_path),
        )

    run.home = home  # type: ignore[attr-defined]
    run.log = log  # type: ignore[attr-defined]
    return run


def _bash_works() -> bool:
    if shutil.which("bash") is None:
        return False
    try:
        return (
            subprocess.run(
                ["bash", "-c", "exit 0"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except OSError:  # pragma: no cover - platform guard
        return False


def _python() -> str:
    import sys

    return sys.executable


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split("\t") for line in log.read_text().splitlines() if line.strip()]


def test_prebake_succeeds_against_the_mirror(harness):
    proc = harness()
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_prebake_installs_from_the_lock_not_a_range(harness):
    """`npm ci` is the pin. `npm install <spec>` re-resolves ~120 transitive
    ranges at build time, which is exactly the non-reproducibility #393 names."""
    harness()
    calls = _calls(harness.log)
    npm = [c for c in calls if c[0].startswith("npm")]
    assert any(c[1] == "ci" for c in npm), npm
    installs = [c for c in npm if c[1] == "install"]
    assert installs == [], f"prebake still range-installs: {installs}"


def test_prebake_makes_our_registry_the_default_for_every_fetching_call(harness):
    """Scoping the registry to @molecule-ai only was the bug: everything else
    (@modelcontextprotocol/sdk, pino, zod, express, ...) fell through to the
    DEFAULT registry, i.e. npmjs.org. Every call that can fetch must now carry
    our registry as the default."""
    harness()
    fetching = [c for c in _calls(harness.log) if c[0].startswith("npm")]
    assert fetching, "script made no npm calls"
    assert {c[-2] for c in fetching} == {f"ENV_registry={pai.MANAGEMENT_MCP_REGISTRY}"}


def test_no_call_ever_sees_a_registry_other_than_ours(harness):
    harness()
    for call in _calls(harness.log):
        registry = call[-2].split("=", 1)[1]
        assert registry in ("", pai.MANAGEMENT_MCP_REGISTRY), call


def test_launch_simulation_runs_without_the_build_time_registry(harness):
    """The build-time default registry is an ENV VAR, so it does NOT survive
    into the runtime. The foreign-HOME self-check must therefore prove the
    launch resolves WITHOUT it -- otherwise the check passes for a reason the
    real concierge boot does not have."""
    harness()
    npx = [c for c in _calls(harness.log) if c[0].startswith("npx")]
    unset = [c for c in npx if c[-2] == "ENV_registry="]
    assert unset, f"no launch-sim invocation ran without npm_config_registry: {npx}"
    # ...and it still used the baked cache, i.e. it is a real offline resolve.
    assert all(c[-1] != "ENV_cache=" for c in unset), unset


def test_prebake_never_names_the_upstream_registry(harness):
    harness()
    blob = harness.log.read_text() + (harness.home / ".npmrc").read_text()
    assert UPSTREAM not in blob


def test_agent_npmrc_stays_scoped_only(harness):
    """Deliberate: a DEFAULT registry written into the agent's ~/.npmrc would
    also redirect the agent's own `npm install` in the workspace at our sparse
    mirror and break ordinary user projects. The build-time default travels as
    an env var instead."""
    harness()
    npmrc = (harness.home / ".npmrc").read_text()
    assert (
        f"{pai.MANAGEMENT_MCP_REGISTRY_SCOPE}:registry={pai.MANAGEMENT_MCP_REGISTRY}"
        in npmrc
    )
    assert not any(
        line.strip().startswith("registry=") for line in npmrc.splitlines()
    ), npmrc


def test_prebake_warms_packuments_for_the_locked_tree(harness):
    """`npm ci` caches TARBALLS but not PACKUMENTS, and the offline launch
    resolves a RANGE — which needs a packument. Without this warm the image
    builds green and the concierge ETARGETs at boot (#1027)."""
    harness()
    added = [
        c for c in _calls(harness.log) if c[0].startswith("npm") and c[1] == "cache"
    ]
    specs = {
        arg for c in added for arg in c[3:] if "@" in arg and not arg.startswith("-")
    }
    lock = json.loads((LOCK_DIR / "package-lock.json").read_text())
    want = {
        (e.get("name") or p.rsplit("node_modules/", 1)[-1]) + "@" + e["version"]
        for p, e in lock["packages"].items()
        if p and e.get("resolved")
    }
    assert want <= specs, sorted(want - specs)[:5]


def test_prebake_seeds_the_npx_cache_for_the_launch_range(harness):
    """The launch runs `npx <pkg>@<range>`; its _npx entry is keyed by the RANGE
    string, a different key from the exact spec."""
    harness()
    # Only `--prefer-offline` invocations SEED (they may fetch); the `--offline`
    # ones are the self-checks, and counting them made this pass vacuously for a
    # script that seeded the exact spec alone.
    seeded = {
        call[call.index("--prefer-offline") + 1]
        for call in _calls(harness.log)
        if call[0].startswith("npx") and "--prefer-offline" in call
    }
    pkg = pai.MANAGEMENT_MCP_NPM_PACKAGE
    assert f"{pkg}@{pai.MANAGEMENT_MCP_COMPATIBLE_RANGE}" in seeded, seeded
    assert f"{pkg}@{pai.MANAGEMENT_MCP_PINNED_VERSION}" in seeded, seeded


# --- fail-loud arms -------------------------------------------------------


def test_mirror_miss_fails_the_build_loudly(harness):
    """A 404 from our registry must end the build. npm has no upstream
    fallback, so the only way this could pass silently is if the script
    swallowed the failure."""
    proc = harness(env={"MOLECULE_STUB_NPM_CI_FAILS": "1"})
    assert proc.returncode != 0
    assert "404" in proc.stderr or "npm error" in proc.stderr


def test_missing_vendored_pin_fails_with_a_named_cause(harness):
    proc = harness(lock_files={})
    assert proc.returncode != 0
    assert "vendored npm pin missing" in proc.stderr


def test_pin_drift_between_constant_and_lock_fails(harness):
    """Bumping MANAGEMENT_MCP_PINNED_VERSION without re-running the mirror must
    not build an image whose lock still names the old version."""
    manifest = json.loads((LOCK_DIR / "package.json").read_text())
    manifest["dependencies"][pai.MANAGEMENT_MCP_NPM_PACKAGE] = "0.0.1-stale"
    proc = harness(
        lock_files={
            "package.json": json.dumps(manifest),
            "package-lock.json": (LOCK_DIR / "package-lock.json").read_text(),
        }
    )
    assert proc.returncode != 0
    assert "pin drift" in proc.stderr


def test_lock_referencing_upstream_fails_the_build(harness):
    """Belt and braces for the one thing a .npmrc cannot override: a `resolved`
    URL pointing at npmjs.org."""
    lock = json.loads((LOCK_DIR / "package-lock.json").read_text())
    lock["packages"]["node_modules/zod"]["resolved"] = (
        f"https://{UPSTREAM}/zod/-/zod-3.25.76.tgz"
    )
    proc = harness(
        lock_files={
            "package.json": (LOCK_DIR / "package.json").read_text(),
            "package-lock.json": json.dumps(lock),
        }
    )
    assert proc.returncode != 0
    assert UPSTREAM in proc.stderr


def test_broken_bake_still_fails_the_build(harness):
    """The pre-existing hard gate must survive the rewrite: if the offline
    launch does not expose the degrade-gate verb, the image must not ship."""
    proc = harness(env={"MOLECULE_STUB_NPX_SILENT": "1"})
    assert proc.returncode != 0
    assert "did not resolve OFFLINE" in proc.stderr
