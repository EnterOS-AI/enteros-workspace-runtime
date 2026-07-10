from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_consumer_runtime_drift.py"
SPEC = importlib.util.spec_from_file_location("check_consumer_runtime_drift", SCRIPT_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


def test_detects_top_level_workspace_runtime_tree(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-core"
    (repo / "workspace").mkdir(parents=True)

    import check_consumer_runtime_drift as guard

    assert [(finding.path, finding.reason) for finding in guard.find_runtime_drift(
        "molecule-core", repo,
        runtime_root=Path(__file__).resolve().parents[1]
    )] == [
        (
            "workspace/",
            "top-level workspace/ runtime tree is forbidden; use the runtime package",
        )
    ]


def test_detects_nested_vendored_molecule_runtime_package(tmp_path: Path) -> None:
    repo = tmp_path / "molecule-ai-workspace-template-hermes"
    (repo / "vendor" / "molecule_runtime").mkdir(parents=True)

    import check_consumer_runtime_drift as guard

    assert [(finding.path, finding.reason) for finding in guard.find_runtime_drift(
        "molecule-ai-workspace-template-hermes", repo,
        runtime_root=Path(__file__).resolve().parents[1]
    )] == [
        (
            "vendor/molecule_runtime/",
            "vendored molecule_runtime/ package is forbidden; import the SSOT package",
        )
    ]


def test_detects_runtime_pin_drift(tmp_path: Path) -> None:
    """DriftFinding fires when .runtime-version differs from SSOT (runtime#53)."""
    repo = tmp_path / "molecule-ai-workspace-template-claude-code"
    repo.mkdir()
    (repo / ".runtime-version").write_text("0.2.1\n")  # stale drift from SSOT 0.3.6

    import check_consumer_runtime_drift as guard

    findings = guard.find_runtime_drift(
        "molecule-ai-workspace-template-claude-code", repo,
        runtime_root=Path(__file__).resolve().parents[1],
    )
    assert len(findings) == 1
    assert findings[0].path == ".runtime-version"
    assert "runtime pin drift" in findings[0].reason
    assert "0.2.1" in findings[0].reason


def test_allows_runtime_pin_matching_ssot(tmp_path: Path) -> None:
    """No drift finding when .runtime-version matches SSOT (even if older)."""
    repo = tmp_path / "molecule-ai-workspace-template-codex"
    repo.mkdir()

    import check_consumer_runtime_drift as guard

    # SSOT 0.3.6 is fresh; use that version so no drift.
    (repo / ".runtime-version").write_text(guard.current_runtime_version(
        Path(__file__).resolve().parents[1]
    ) + "\n")
    (repo / "requirements.txt").write_text("molecule-ai-workspace-runtime==0.3.6\n")
    (repo / "README.md").write_text("Mount files at /workspace and import molecule_runtime.\n")

    assert guard.find_runtime_drift(
        "molecule-ai-workspace-template-codex", repo,
        runtime_root=Path(__file__).resolve().parents[1],
    ) == []


def test_clone_consumers_retries_on_transient_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """clone_consumers retries clone on transient failure (RCA #52 Finding 2)."""
    import subprocess

    call_count = 0

    def flaky_clone(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return type("Result", (), {"returncode": 128, "stderr": "transient error", "stdout": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", flaky_clone)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    import check_consumer_runtime_drift as guard
    guard.clone_consumers(workdir, ("molecule-core",), gitea_url="https://git.moleculesai.app", token="fake-token")
    assert call_count == 3, f"expected 3 attempts, got {call_count}"


def test_seo_agent_is_exempt_not_enumerated() -> None:
    """seo-agent must be explicitly EXEMPT (config/prompts template, no wheel),
    never silently dropped from the consumer set (runtime drift blind-spot fix)."""
    import check_consumer_runtime_drift as guard

    assert "molecule-ai-workspace-template-seo-agent" in guard.EXEMPT_CONSUMERS
    assert "molecule-ai-workspace-template-seo-agent" not in guard.DEFAULT_CONSUMERS


def test_retired_templates_are_exempt_not_enumerated() -> None:
    """google-adk + crewai were RETIRED 2026-07-09 (runtime#264/#265, sdk#80,
    core#3730) — their template repos are archived and no longer wheel-bumped, so
    their frozen .runtime-version pin is EXPECTED, not drift. They must be EXEMPT
    (explicitly accounted-for), never in DEFAULT_CONSUMERS — which would red every
    runtime PR on their stale pins. (Was the reverse assertion pre-retirement.)"""
    import check_consumer_runtime_drift as guard

    for repo in (
        "molecule-ai-workspace-template-google-adk",
        "molecule-ai-workspace-template-crewai",
    ):
        assert repo in guard.EXEMPT_CONSUMERS, f"retired {repo} should be EXEMPT"
        assert repo not in guard.DEFAULT_CONSUMERS, (
            f"retired {repo} must not be in DEFAULT_CONSUMERS (would false-red drift)"
        )


def test_reconcile_flags_unenumerated_pinned_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """A template repo that carries .runtime-version but is neither enumerated
    nor exempt is surfaced as unaccounted-for (the loud blind-spot tripwire)."""
    import check_consumer_runtime_drift as guard

    monkeypatch.setattr(
        guard,
        "_org_template_repos",
        lambda gitea_url, token, org="molecule-ai": [
            "molecule-ai-workspace-template-crewai",  # enumerated -> ok
            "molecule-ai-workspace-template-seo-agent",  # exempt -> ok
            "molecule-ai-workspace-template-newruntime",  # NOT accounted for
        ],
    )
    monkeypatch.setattr(
        guard,
        "_repo_has_runtime_version",
        lambda repo, gitea_url, token, org="molecule-ai": repo
        == "molecule-ai-workspace-template-newruntime",
    )

    unaccounted = guard.reconcile_org_consumers(
        guard.DEFAULT_CONSUMERS, gitea_url="https://git.moleculesai.app", token="fake-token"
    )
    assert unaccounted == ["molecule-ai-workspace-template-newruntime"]


def test_reconcile_clean_when_all_accounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """No unaccounted repos when every pinned template is enumerated or exempt."""
    import check_consumer_runtime_drift as guard

    monkeypatch.setattr(
        guard,
        "_org_template_repos",
        lambda gitea_url, token, org="molecule-ai": [
            "molecule-ai-workspace-template-crewai",
            "molecule-ai-workspace-template-seo-agent",
        ],
    )
    monkeypatch.setattr(
        guard,
        "_repo_has_runtime_version",
        lambda repo, gitea_url, token, org="molecule-ai": True,
    )

    assert (
        guard.reconcile_org_consumers(
            guard.DEFAULT_CONSUMERS, gitea_url="https://git.moleculesai.app", token="fake-token"
        )
        == []
    )



def test_org_listing_403_raises_reconcile_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token-scope 403 on /orgs/{org}/repos is a CONFIG gap (needs
    read:organization), surfaced as ReconcileUnavailable, not a generic
    RuntimeError — so main() can warn+skip instead of painting main red."""
    import urllib.error
    import urllib.request

    import check_consumer_runtime_drift as guard

    def fake_urlopen(req, timeout=15):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {},
            io.BytesIO(b'{"message":"token does not have at least one of required '
                       b'scope(s), required=[read:organization]"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(guard.ReconcileUnavailable):
        guard._org_template_repos("https://git.moleculesai.app", "scopeless-token")


def test_org_listing_skips_archived_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Archived template repos are read-only: their .runtime-version pin is
    frozen and a propagation bump PR can never land, so they are NOT live wheel
    consumers and must not trip the blind-spot reconcile. Regression for the
    2026-07-05 main red: the four retired-runtime templates (langgraph/autogen/
    deepagents/gemini-cli, archived 2026-07-04) still carry frozen pins and
    painted runtime main red until the org scan learned to skip archived."""
    import json
    import urllib.request

    import check_consumer_runtime_drift as guard

    batch = [
        {"name": "molecule-ai-workspace-template-crewai", "archived": False},
        {"name": "molecule-ai-workspace-template-langgraph", "archived": True},
        {"name": "molecule-ai-workspace-template-gemini-cli", "archived": True},
        # archived flag absent -> treated as live (defensive default)
        {"name": "molecule-ai-workspace-template-hermes"},
        # non-template org repo, never included regardless
        {"name": "molecule-core", "archived": False},
    ]

    class FakeResp(io.BytesIO):
        status = 200

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

    def fake_urlopen(req, timeout=15):  # noqa: ANN001
        return FakeResp(json.dumps(batch).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    repos = guard._org_template_repos("https://git.moleculesai.app", "some-token")
    assert repos == [
        "molecule-ai-workspace-template-crewai",
        "molecule-ai-workspace-template-hermes",
    ], "archived template repos must be excluded from the org consumer scan"


def test_main_warns_and_skips_reconcile_on_scope_gap(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path) -> None:
    """When the org-scan reconcile is unavailable (token scope), main() must NOT
    fail: it warns and falls through to the pin-drift check, which passes here."""
    import check_consumer_runtime_drift as guard

    # reconcile blows up with the config-gap signal
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise guard.ReconcileUnavailable("org repo listing requires read:organization (HTTP 403)")

    monkeypatch.setattr(guard, "reconcile_org_consumers", boom)
    # Build a clean --root so the clone path is skipped and no findings arise.
    root = tmp_path / "consumers"
    for repo in guard.DEFAULT_CONSUMERS:
        (root / repo).mkdir(parents=True)
    # current_runtime_version would hit the network; force a known SSOT and make
    # every consumer carry a matching pin so there is zero drift.
    monkeypatch.setattr(guard, "current_runtime_version", lambda *a, **k: "9.9.9")
    for repo in guard.DEFAULT_CONSUMERS:
        (root / repo / ".runtime-version").write_text("9.9.9\n")

    monkeypatch.setenv("GITEA_TOKEN", "scopeless-token")
    # --root path skips reconcile by design; to exercise the scope-gap branch we
    # must run the live (no --root) reconcile gate. Patch clone to use our root.
    monkeypatch.setattr(
        guard, "clone_consumers",
        lambda workdir, repos, *, gitea_url, token: {r: root / r for r in repos},
    )
    # NB: pass a non-empty argv; main() does `parse_args(argv or sys.argv[1:])`,
    # so [] would fall through to pytest's own argv. An explicit --gitea-url keeps
    # the reconcile gate active (no --root / no --repo) while being a real argv.
    rc = guard.main(["--gitea-url", "https://git.moleculesai.app"])
    err = capsys.readouterr().err
    assert rc == 0, "scope-gap reconcile must not fail the guard"
    assert "skipping org-scan reconcile" in err


def test_clone_consumers_never_puts_token_in_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GIT_ASKPASS path: token must not appear in git clone argv or remote URL (runtime#86).

    Re-introduced on the runtime#86 branch after Kimi's prior commit
    (061716f) was reverted twice on main; the gate test (see
    tests/test_workflow_no_token_in_url.py) makes a future reversion
    red-by-default in CI.
    """
    import subprocess

    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture_run(*args: object, **kwargs: object) -> object:
        captured.append((args, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", capture_run)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    import check_consumer_runtime_drift as guard
    guard.clone_consumers(workdir, ("molecule-core",), gitea_url="https://git.moleculesai.app", token="s3cr3t-t0k3n")

    assert len(captured) == 1
    cmd = captured[0][0][0]
    env = captured[0][1].get("env") or {}
    cmd_str = " ".join(str(c) for c in cmd)
    assert "s3cr3t-t0k3n" not in cmd_str, "token leaked into subprocess argv"
    assert "x-access-token" not in cmd_str, "username leaked into subprocess argv"
    assert env.get("GIT_ASKPASS") is not None, "GIT_ASKPASS not set in clone env"


# ---------------------------------------------------------------------------
# Propagation-lag tolerance (block only on STUCK consumers; advisory in-flight)
# ---------------------------------------------------------------------------


def _pin_drift_finding(repo: str, pinned: str = "0.3.15", ssot: str = "0.3.20"):
    import check_consumer_runtime_drift as guard

    return guard.DriftFinding(
        repo=repo,
        path=".runtime-version",
        reason=f"runtime pin drift: pinned={pinned}, SSOT={ssot}",
    )


def test_extract_bump_target_matches_title_and_branch() -> None:
    """The in-flight probe recognises the runtime#91 bot's PR by title OR branch."""
    import check_consumer_runtime_drift as guard

    assert guard._extract_bump_target(
        {"title": "chore(runtime): bump .runtime-version to 0.3.20", "head": {"ref": "x"}}
    ) == "0.3.20"
    # Title hand-edited but canonical head branch present -> still recognised.
    assert guard._extract_bump_target(
        {"title": "please bump", "head": {"ref": "bump/runtime-0.3.19"}}
    ) == "0.3.19"
    # Unrelated PR -> not a bump PR.
    assert guard._extract_bump_target(
        {"title": "feat: add thing", "head": {"ref": "feature/thing"}}
    ) is None


def test_open_bump_pr_target_returns_highest_advancing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the highest open bump target strictly greater than the lagging pin,
    ignoring non-bump PRs and any target that would not advance the pin."""
    import io
    import json
    import urllib.request

    import check_consumer_runtime_drift as guard

    prs = [
        {"title": "feat: unrelated", "head": {"ref": "feature/x"}},
        {"title": "chore(runtime): bump .runtime-version to 0.3.19", "head": {"ref": "bump/runtime-0.3.19"}},
        {"title": "chore(runtime): bump .runtime-version to 0.3.20", "head": {"ref": "bump/runtime-0.3.20"}},
        # A stale bump to a version <= pin must NOT count as advancing.
        {"title": "chore(runtime): bump .runtime-version to 0.3.10", "head": {"ref": "bump/runtime-0.3.10"}},
    ]

    class FakeResp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=15: FakeResp(json.dumps(prs).encode())
    )
    got = guard._open_bump_pr_target(
        "molecule-ai-workspace-template-hermes",
        pinned="0.3.15",
        gitea_url="https://git.example.test",
        token="tok",
    )
    assert got == "0.3.20"


def test_open_bump_pr_target_none_when_no_advancing_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """No open bump PR advancing the pin -> None (the STUCK signal)."""
    import io
    import json
    import urllib.request

    import check_consumer_runtime_drift as guard

    prs = [{"title": "feat: unrelated", "head": {"ref": "feature/x"}}]

    class FakeResp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=15: FakeResp(json.dumps(prs).encode())
    )
    assert (
        guard._open_bump_pr_target(
            "molecule-core", pinned="0.3.15", gitea_url="https://git.example.test", token="tok"
        )
        is None
    )


def test_open_bump_pr_target_raises_on_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed open-PR listing surfaces as PropagationStatusUnavailable so the
    caller can fail-soft to ADVISORY (never block on an undeterminable signal)."""
    import urllib.request

    import check_consumer_runtime_drift as guard

    def boom(req, timeout=15):  # noqa: ANN001
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(guard.PropagationStatusUnavailable):
        guard._open_bump_pr_target(
            "molecule-core", pinned="0.3.15", gitea_url="https://git.example.test", token="tok"
        )


def test_classify_lag_with_inflight_pr_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """(1) lag + open bump PR -> ADVISORY, not a failure."""
    import check_consumer_runtime_drift as guard

    monkeypatch.setattr(
        guard, "_open_bump_pr_target", lambda repo, **kw: "0.3.20"
    )
    finding = _pin_drift_finding("molecule-ai-workspace-template-hermes")
    blocking, advisory = guard.classify_pin_drift(
        [finding],
        pins={"molecule-ai-workspace-template-hermes": "0.3.15"},
        gitea_url="https://git.example.test",
        token="tok",
    )
    assert blocking == []
    assert len(advisory) == 1 and "IN FLIGHT" in advisory[0]


def test_classify_lag_without_inflight_pr_is_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """(2) lag + NO open bump PR -> HARD FAILURE (blocking), the stuck signal."""
    import check_consumer_runtime_drift as guard

    monkeypatch.setattr(guard, "_open_bump_pr_target", lambda repo, **kw: None)
    finding = _pin_drift_finding("molecule-core")
    blocking, advisory = guard.classify_pin_drift(
        [finding],
        pins={"molecule-core": "0.3.15"},
        gitea_url="https://git.example.test",
        token="tok",
    )
    assert blocking == [finding]
    assert advisory == []


def test_classify_all_current_passes() -> None:
    """(3) no drift findings -> nothing blocking, nothing advisory."""
    import check_consumer_runtime_drift as guard

    assert guard.classify_pin_drift(
        [], pins={}, gitea_url="https://git.example.test", token="tok"
    ) == ([], [])


def test_classify_absent_token_is_advisory_failsoft(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token -> can't check in-flight status -> ADVISORY (fail-soft), and the
    network probe is never even attempted."""
    import check_consumer_runtime_drift as guard

    def must_not_call(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("must not probe PRs when no token is available")

    monkeypatch.setattr(guard, "_open_bump_pr_target", must_not_call)
    finding = _pin_drift_finding("molecule-core")
    blocking, advisory = guard.classify_pin_drift(
        [finding], pins={"molecule-core": "0.3.15"}, gitea_url="https://git.example.test", token=""
    )
    assert blocking == []
    assert len(advisory) == 1 and "without a token" in advisory[0]


def test_classify_probe_failure_is_advisory_failsoft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe raises PropagationStatusUnavailable -> ADVISORY (fail-soft), not block."""
    import check_consumer_runtime_drift as guard

    def boom(repo, **kw):  # noqa: ANN001, ANN003
        raise guard.PropagationStatusUnavailable("cannot list open PRs: 502")

    monkeypatch.setattr(guard, "_open_bump_pr_target", boom)
    finding = _pin_drift_finding("molecule-core")
    blocking, advisory = guard.classify_pin_drift(
        [finding], pins={"molecule-core": "0.3.15"}, gitea_url="https://git.example.test", token="tok"
    )
    assert blocking == []
    assert len(advisory) == 1 and "could not determine" in advisory[0]


def test_classify_vendoring_finding_always_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forbidden workspace/ tree or vendored molecule_runtime/ package is NOT a
    propagation-lag concern and must ALWAYS block, regardless of in-flight PRs."""
    import check_consumer_runtime_drift as guard

    def must_not_call(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("vendoring findings must not trigger a PR probe")

    monkeypatch.setattr(guard, "_open_bump_pr_target", must_not_call)
    vendoring = guard.DriftFinding(
        repo="molecule-core",
        path="workspace/",
        reason="top-level workspace/ runtime tree is forbidden; use the runtime package",
    )
    blocking, advisory = guard.classify_pin_drift(
        [vendoring], pins={}, gitea_url="https://git.example.test", token="tok"
    )
    assert blocking == [vendoring]
    assert advisory == []


def _make_root_with_pins(guard, tmp_path, lagging: dict[str, str], ssot: str):
    """Build a --root tree of DEFAULT_CONSUMERS; each carries `ssot` unless
    overridden in `lagging` (repo -> pinned)."""
    root = tmp_path / "consumers"
    for repo in guard.DEFAULT_CONSUMERS:
        d = root / repo
        d.mkdir(parents=True)
        (d / ".runtime-version").write_text((lagging.get(repo, ssot)) + "\n")
    return root


def test_main_pin_lag_inflight_is_green(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path) -> None:
    """End-to-end (main): a consumer lags the SSOT but has an in-flight bump PR ->
    the gate exits 0 (green) with an advisory, not red."""
    import check_consumer_runtime_drift as guard

    monkeypatch.setattr(guard, "current_runtime_version", lambda *a, **k: "9.9.9")
    laggard = "molecule-core"
    root = _make_root_with_pins(guard, tmp_path, {laggard: "9.9.8"}, "9.9.9")
    monkeypatch.setattr(
        guard, "_open_bump_pr_target",
        lambda repo, **kw: "9.9.9" if repo == laggard else None,
    )
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    rc = guard.main(["--root", str(root)])
    out = capsys.readouterr()
    assert rc == 0, "in-flight lag must be advisory/green, not red"
    assert "IN FLIGHT" in out.err
    assert "propagation IN FLIGHT" in out.out


def test_main_pin_lag_stuck_is_red(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path) -> None:
    """End-to-end (main): a consumer lags the SSOT and has NO in-flight bump PR ->
    the gate exits 1 (red) — the genuinely-stuck signal is preserved."""
    import check_consumer_runtime_drift as guard

    monkeypatch.setattr(guard, "current_runtime_version", lambda *a, **k: "9.9.9")
    laggard = "molecule-core"
    root = _make_root_with_pins(guard, tmp_path, {laggard: "9.9.8"}, "9.9.9")
    monkeypatch.setattr(guard, "_open_bump_pr_target", lambda repo, **kw: None)
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    rc = guard.main(["--root", str(root)])
    err = capsys.readouterr().err
    assert rc == 1, "stuck consumer (lag + no bump PR) must stay red"
    assert "STUCK" in err
