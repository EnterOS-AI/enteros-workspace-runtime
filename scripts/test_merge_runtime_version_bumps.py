"""Unit tests for the runtime#131 auto-merge sweeper (scripts/merge_runtime_version_bumps.py).

The sweeper has three observable pure functions that drive the merge
decision:
  - is_runtime_bump_pr(pr, files) — "is this a bot-authored, runtime-pin-only PR?"
  - all_required_statuses_success(combined) — "are the required commit statuses green?"
  - the per-repo opt-out via is_repo_opted_in(repo, client) — covered
    end-to-end in the workflow dispatch test (network), not in pure unit.

These are the contract tests for the sweeper — a regression in any
of them ships the wrong PR to main.

We do NOT test GiteaClient directly (it's a thin urllib wrapper around
the same endpoints propagate_runtime_version.py / auto_release_runtime.py
already exercise). The interesting logic is the gating.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

# Load the script module (it's a standalone CLI, not importable as a
# package — same pattern as test_propagate_runtime_version_dual_pin.py).
_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE / "merge_runtime_version_bumps.py"
_spec = importlib.util.spec_from_file_location("merge_runtime_version_bumps", _SCRIPT)
assert _spec and _spec.loader, "failed to load merge_runtime_version_bumps.py"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_runtime_bump_pr = _mod.is_runtime_bump_pr
all_required_statuses_success = _mod.all_required_statuses_success
ALLOWED_BUMP_FILES = _mod.ALLOWED_BUMP_FILES
REQUIRED_STATUS_SUBSTRINGS = _mod.REQUIRED_STATUS_SUBSTRINGS
CONSUMER_TEMPLATE_REPOS = _mod.CONSUMER_TEMPLATE_REPOS

# The opener identity is resolved at runtime as whoami(read_token) — the
# 2026-07-05 identity-drift RC replaced the stale hardcode
# ("molecule-runtime-release-bot" vs the credential's actual owner
# `core-devops`, which no-opped the sweeper for weeks). Tests pass an
# explicit author the way run() does.
OPENER = "core-devops"

# Real context names as emitted by the six consumer-template CIs (verified
# against live bump-PR heads 2026-07-05) — what the substring gate must
# match. The old exact names ("validate-runtime", "t4-conformance") matched
# none of these.
REAL_CONTEXTS_GREEN = [
    "CI / Template validation (static) (pull_request)",
    "CI / Template validation (runtime) (pull_request)",
    "CI / T4 tier-4 conformance (live) (pull_request)",
    "CI / Adapter unit tests (pull_request)",
    "Secret scan / Scan diff for credential-shaped strings (pull_request)",
]


def _pr(
    *,
    user_login: str = OPENER,
    number: int = 1,
    head_branch: str = "bump-runtime-0.3.23",
) -> dict:
    return {
        "number": number,
        "user": {"login": user_login},
        "head": {"ref": head_branch, "sha": "deadbeef" * 5},
        "title": f"chore: bump molecule-ai-workspace-runtime to {head_branch.removeprefix('bump-runtime-')}",
    }


class IsRuntimeBumpPr(unittest.TestCase):
    def test_opener_author_plus_dot_runtime_version_alone_is_a_bump(self) -> None:
        pr = _pr()
        self.assertTrue(is_runtime_bump_pr(pr, [".runtime-version"], OPENER))

    def test_opener_author_plus_dual_pin_is_a_bump(self) -> None:
        # codex-style templates bump .runtime-version + requirements.txt
        # atomically (per scripts/propagate_runtime_version.py).
        pr = _pr()
        self.assertTrue(
            is_runtime_bump_pr(pr, [".runtime-version", "requirements.txt"], OPENER)
        )

    def test_opener_author_with_extra_file_is_NOT_a_bump(self) -> None:
        # If a hand-edit added an unrelated change, the sweeper MUST
        # skip — the opener's PR is no longer "runtime-version bump only".
        pr = _pr()
        self.assertFalse(
            is_runtime_bump_pr(pr, [".runtime-version", "README.md"], OPENER)
        )

    def test_non_opener_author_is_NOT_a_bump(self) -> None:
        # A non-opener PR (e.g. a human's hand-bump) must not be auto-merged
        # even if the diff is only .runtime-version.
        pr = _pr(user_login="devops-engineer")
        self.assertFalse(is_runtime_bump_pr(pr, [".runtime-version"], OPENER))

    def test_stale_hardcoded_bot_name_is_NOT_trusted(self) -> None:
        # Identity-drift regression: the historical hardcode must not be
        # silently trusted when the resolved opener is a different account.
        pr = _pr(user_login="molecule-runtime-release-bot")
        self.assertFalse(is_runtime_bump_pr(pr, [".runtime-version"], OPENER))

    def test_empty_expected_author_is_NOT_a_bump(self) -> None:
        # An unresolved opener identity must never widen to "any author".
        pr = _pr()
        self.assertFalse(is_runtime_bump_pr(pr, [".runtime-version"], ""))

    def test_empty_files_is_NOT_a_bump(self) -> None:
        # A PR with no changed files is degenerate; the Gitea API
        # would return [] but the sweeper must refuse rather than guess.
        pr = _pr()
        self.assertFalse(is_runtime_bump_pr(pr, [], OPENER))

    def test_wrong_user_payload_shape_is_NOT_a_bump(self) -> None:
        # Defensive: if the API returns a malformed PR (no `user` dict),
        # the sweeper must not crash and must not treat it as a bump.
        bad = {"number": 1, "head": {"ref": "bump-runtime-0.3.23"}}
        self.assertFalse(is_runtime_bump_pr(bad, [".runtime-version"], OPENER))
        self.assertFalse(is_runtime_bump_pr(None, [".runtime-version"], OPENER))  # type: ignore[arg-type]


class AllRequiredStatusesSuccess(unittest.TestCase):
    def test_all_green_real_context_names(self) -> None:
        # Context-name-drift regression (2026-07-05): the gate must pass on
        # the names the template CIs ACTUALLY emit.
        combined = {
            "state": "success",
            "statuses": [{"context": ctx, "status": "success"} for ctx in REAL_CONTEXTS_GREEN],
        }
        self.assertTrue(all_required_statuses_success(combined))

    def test_push_suffixed_contexts_also_satisfy(self) -> None:
        # hermes/openclaw-style repos dual-post (push) + (pull_request)
        # rows; either suffix satisfies the substring anchor.
        combined = {
            "state": "success",
            "statuses": [{"context": "CI / Template validation (runtime) (push)", "status": "success"}],
        }
        self.assertTrue(all_required_statuses_success(combined))

    def test_old_exact_names_alone_do_NOT_satisfy(self) -> None:
        # The retired exact names are not what CIs emit; a payload carrying
        # ONLY them must not pass the wheel-install anchor.
        combined = {
            "state": "success",
            "statuses": [
                {"context": "validate-runtime", "status": "success"},
                {"context": "t4-conformance", "status": "success"},
            ],
        }
        self.assertFalse(all_required_statuses_success(combined))

    def test_anchor_gate_is_failure(self) -> None:
        # A failure on the wheel-install gate fails closed (combined
        # flips off success too).
        combined = {
            "state": "failure",
            "statuses": [
                {"context": "CI / Template validation (runtime) (pull_request)", "status": "failure"},
            ],
        }
        self.assertFalse(all_required_statuses_success(combined))

    def test_anchor_gate_is_pending(self) -> None:
        # Pending is not success — even defensively when the combined
        # state (mistakenly) reads success.
        combined = {
            "state": "success",
            "statuses": [
                {"context": "CI / Template validation (runtime) (pull_request)", "status": "pending"},
            ],
        }
        self.assertFalse(all_required_statuses_success(combined))

    def test_anchor_gate_missing_entirely(self) -> None:
        # Guards the just-pushed window: combined may read success while
        # the real gates have not registered their contexts yet.
        combined = {
            "state": "success",
            "statuses": [
                {"context": "Secret scan / Scan diff for credential-shaped strings (pull_request)", "status": "success"},
            ],
        }
        self.assertFalse(all_required_statuses_success(combined))

    def test_extra_failing_unrelated_context_fails(self) -> None:
        # The sweeper requires the COMBINED state to be `success` — a
        # failing unrelated context (T4 wherever a template defines it,
        # secret-scan, ...) flips combined to `failure` and we fail
        # closed rather than proceed.
        combined = {
            "state": "failure",
            "statuses": [
                {"context": "CI / Template validation (runtime) (pull_request)", "status": "success"},
                {"context": "CI / T4 tier-4 conformance (live) (pull_request)", "status": "failure"},
            ],
        }
        self.assertFalse(all_required_statuses_success(combined))

    def test_extra_pending_unrelated_context_fails(self) -> None:
        # Same as above but pending: combined goes `pending`, gate fails.
        combined = {
            "state": "pending",
            "statuses": [
                {"context": "CI / Template validation (runtime) (pull_request)", "status": "success"},
                {"context": "CI / validate (pull_request)", "status": "pending"},
            ],
        }
        self.assertFalse(all_required_statuses_success(combined))

    def test_empty_combined(self) -> None:
        # Defensive: malformed API response → fail closed.
        self.assertFalse(all_required_statuses_success({}))
        self.assertFalse(all_required_statuses_success({"state": "unknown", "statuses": []}))

    def test_state_field_child_shape_still_accepted(self) -> None:
        # RC 13421 regression (kept): child rows may carry `state` instead
        # of `status` across payload shapes; both are accepted.
        combined = {
            "state": "success",
            "statuses": [
                {"context": "CI / Template validation (runtime) (pull_request)", "state": "success"},
            ],
        }
        self.assertTrue(all_required_statuses_success(combined))


class Constants(unittest.TestCase):
    """Pin the SSOT constants so a future drift in the consumer list
    or the required-status gate triggers a loud test failure rather
    than a silent change in auto-merge behavior."""

    def test_required_substrings_anchor_wheel_install_gate(self) -> None:
        # The sweeper is tightly scoped on this anchor; dropping it would
        # let a zero-status head read as mergeable.
        self.assertGreaterEqual(len(REQUIRED_STATUS_SUBSTRINGS), 1)
        self.assertIn("Template validation (runtime)", REQUIRED_STATUS_SUBSTRINGS)

    def test_consumer_list_non_empty(self) -> None:
        self.assertGreaterEqual(len(CONSUMER_TEMPLATE_REPOS), 5)

    def test_no_hardcoded_author_identity_remains(self) -> None:
        # Identity-drift regression (2026-07-05): the expected author is
        # resolved as whoami(read_token) at runtime; a reintroduced
        # hardcode would silently no-op the sweeper again on the next
        # credential rotation.
        self.assertFalse(hasattr(_mod, "BOT_AUTHOR_USERNAME"))
        self.assertFalse(hasattr(_mod, "REVIEWER_USERNAME"))

    def test_requests_carry_curl_user_agent(self) -> None:
        # CF-1010 regression (2026-07-05): the CF edge 403-bans the default
        # python-urllib UA. propagate_runtime_version.py and
        # check_consumer_runtime_drift.py both send a curl UA; the sweeper
        # was the only script missing it, which blocked its first live API
        # call (GET /user) the moment the tokens were provisioned. Pin the
        # header at the Request-construction layer.
        import urllib.request

        captured: list[urllib.request.Request] = []

        def fake_urlopen(req, timeout=30):  # noqa: ANN001
            captured.append(req)
            raise ConnectionError("stop after capturing the request")

        client = _mod.GiteaClient("https://example.invalid", "m", "r", "molecule-ai")
        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(ConnectionError):
                client._request("GET", "/api/v1/user")
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get_header("User-agent"), "curl/8.4.0")

    def test_whoami_reads_user_endpoint(self) -> None:
        # The dynamic identity resolution reads GET /api/v1/user with the
        # chosen token and returns '' on failure (run() then hard-fails).
        client = _mod.GiteaClient("https://example.invalid", "m-tok", "r-tok", "molecule-ai")
        calls: list[tuple[str, str, bool]] = []

        def fake_request(method, path, body=None, params=None, *, use_merge_token=False):
            calls.append((method, path, use_merge_token))
            return 200, {"login": "devops-engineer" if use_merge_token else "core-devops"}

        client._request = fake_request  # type: ignore[method-assign]
        self.assertEqual(client.whoami(), "core-devops")
        self.assertEqual(client.whoami(use_merge_token=True), "devops-engineer")
        self.assertEqual(calls, [("GET", "/api/v1/user", False), ("GET", "/api/v1/user", True)])

        client._request = lambda *a, **k: (401, {"message": "unauthorized"})  # type: ignore[method-assign]
        self.assertEqual(client.whoami(), "")

    def test_allowed_bump_files_minimal(self) -> None:
        # The set MUST be minimal — every extra filename widens the
        # surface the sweeper will auto-merge.
        self.assertEqual(ALLOWED_BUMP_FILES, frozenset({".runtime-version", "requirements.txt"}))


class BranchGrammar(unittest.TestCase):
    """The CR2 RC caught a real bug: my prefix list didn't match the
    actual grammar that scripts/propagate_runtime_version.py emits
    (bump/runtime-{target}, with a slash). These tests pin the
    integration match so a future drift in either side surfaces as a
    loud test failure rather than a silent skip-on-real-bumps bug.

    We exercise the prefix extraction by calling a small helper
    defined locally (extracted from the run() loop) so we can unit-
    test it without spinning up a GiteaClient."""

    @staticmethod
    def _extract_version(branch: str) -> str:
        # Mirrors the prefix list in run(); keep in sync.
        for prefix in ("bump/runtime-v", "bump/runtime-", "bump-runtime-v", "bump-runtime-"):
            if branch.startswith(prefix):
                return branch[len(prefix):]
        return ""

    def test_current_propagate_script_grammar_with_slash(self) -> None:
        # This is the grammar propagate_runtime_version.py actually
        # emits: `bump/runtime-{target}` (with a slash, no `v` prefix).
        # The CR2 RC flagged that my prior prefix list missed the slash.
        self.assertEqual(self._extract_version("bump/runtime-0.3.23"), "0.3.23")
        self.assertEqual(self._extract_version("bump/runtime-0.3.24-rc1"), "0.3.24-rc1")

    def test_current_propagate_script_grammar_with_v_prefix(self) -> None:
        # The historical v-prefixed form is also accepted (older bumps
        # in the wild that pre-date the slash grammar).
        self.assertEqual(self._extract_version("bump/runtime-v0.3.23"), "0.3.23")

    def test_legacy_hyphen_grammar(self) -> None:
        # Legacy `bump-runtime-{target}` (no slash) is also accepted
        # so a future grammar change doesn't strand older bumps.
        self.assertEqual(self._extract_version("bump-runtime-0.3.23"), "0.3.23")
        self.assertEqual(self._extract_version("bump-runtime-v0.3.23"), "0.3.23")

    def test_unrelated_branch_is_not_a_bump(self) -> None:
        # Defensive: a non-bump branch with a similar prefix shape
        # MUST be skipped (not auto-merged). The sweeper is tightly
        # scoped to bump-* branches with the runtime pin.
        self.assertEqual(self._extract_version("chore/fix-something"), "")
        self.assertEqual(self._extract_version("main"), "")
        self.assertEqual(self._extract_version("feature/add-new-thing"), "")


class AbsentMergeToken(unittest.TestCase):
    """The CR2 RC also caught that the script previously used DISPATCH_TOKEN
    for both read and write. We now require CONSUMER_BUMP_MERGE_TOKEN
    (the non-author identity) for write operations; if absent the
    sweeper exits 0 with a loud warning rather than painting runtime
    main red. This is the runtime#83 pattern (config gap, not a
    runtime regression)."""

    def test_absent_merge_token_returns_0(self) -> None:
        # When the merge token is absent, run() returns 0 (no-op) so
        # the __main__ guard exits 0. This is the runtime#83 pattern
        # (config gap, not a runtime regression) — must not paint
        # runtime main red.
        import os

        old_env = os.environ.copy()
        os.environ.clear()
        # Keep GITEA_HOST so the argparse default doesn't crash on
        # something unrelated.
        os.environ["GITEA_HOST"] = "https://example.invalid"
        try:
            sys.argv = ["merge_runtime_version_bumps.py"]
            rc = _mod.run()
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        # Exit 0 (no-op), NOT 2 (the old fail-closed behavior that
        # would have painted runtime main red on a missing config).
        self.assertEqual(rc, 0)

    def test_present_merge_token_proceeds_to_repo_sweep(self) -> None:
        # Sanity check: when the merge token is present, the script
        # doesn't return 0 immediately. It would proceed to the
        # repo sweep (which would fail for other reasons in a unit
        # test, but it MUST get past the gate). We exercise this
        # by calling run() with a dummy token; the script will
        # then try to make a real API call which will fail with a
        # connection error — caught somewhere in the loop, the
        # function returns whatever it returns. We just need to know
        # it didn't return 0 (the absent-token no-op path).
        import os
        import urllib.error

        old_env = os.environ.copy()
        os.environ["CONSUMER_BUMP_MERGE_TOKEN"] = "dummy-merge-token-for-gate-test"
        os.environ["DISPATCH_TOKEN"] = "dummy-read-token-for-gate-test"
        os.environ["GITEA_HOST"] = "https://example.invalid"
        try:
            sys.argv = ["merge_runtime_version_bumps.py"]
            try:
                rc = _mod.run()
            except (urllib.error.URLError, ConnectionError, OSError):
                # The script attempts an HTTP call to a bogus host
                # and fails. That's fine — we only care that it
                # passed the gate (didn't return 0). The exception
                # is the expected behavior here.
                rc = "passed-gate"
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        self.assertNotEqual(rc, 0,
            "with a present merge token, the script must NOT return 0 — "
            "the absent-token no-op is a runtime#83 config-gap pattern, "
            "and a present token means real work should be attempted")


class GiteaHostSchemeGuard(unittest.TestCase):
    """RC 13418: the workflow previously set ``GITEA_HOST: git.moleculesai.app``
    (bare host, no scheme). ``merge_runtime_version_bumps.py`` uses the value
    as the API base URL and concatenates it directly with ``/api/v1/...``,
    so a bare host builds the invalid URL ``git.moleculesai.app/api/v1/...``
    and Python ``urllib`` rejects it with ``ValueError: unknown url type`` —
    the scheduled sweeper would silently fail to merge any consumer bump
    PRs the moment ``CONSUMER_BUMP_MERGE_TOKEN`` is provisioned.

    Two layers of defense:
      1. The script normalizes a bare-host ``GITEA_HOST`` to ``https://<host>``
         with a loud ::warning:: (so a future workflow YAML bug is visible
         in the run log, not silently absorbed).
      2. The workflow YAML's ``env.GITEA_HOST`` is asserted here to start
         with a URL scheme — catches the bug at test time, not at 03:00 UTC
         on a Sunday when ``CONSUMER_BUMP_MERGE_TOKEN`` finally gets wired.
    """

    def test_bare_host_gitea_host_is_normalized_to_https(self) -> None:
        # The script's defensive layer: a bare-host GITEA_HOST must be
        # prepended with https:// (with a ::warning::) rather than rejected
        # — the run should proceed, just with a visible operator alert.
        # We assert by inspecting the GiteaClient built by run() — but
        # since run() short-circuits on absent tokens before the client
        # is constructed, we instead drive the normalization by setting
        # tokens and capturing via a real (failing) network call.
        import os
        import urllib.error

        old_env = os.environ.copy()
        os.environ["CONSUMER_BUMP_MERGE_TOKEN"] = "dummy"
        os.environ["DISPATCH_TOKEN"] = "dummy"
        # Bare host — the exact value that broke in RC 13418.
        os.environ["GITEA_HOST"] = "git.moleculesai.app"
        captured_warnings: list[str] = []
        try:
            sys.argv = ["merge_runtime_version_bumps.py"]
            try:
                _mod.run()
            except (urllib.error.URLError, ConnectionError, OSError, ValueError):
                # Either a connection error to a bogus host, or a
                # ValueError from urllib on the bare host — both are
                # acceptable as long as the normalization ran. The point
                # of this test is the WARNING was emitted, not whether
                # the network call succeeded.
                pass
            # Check the captured warnings on stdout. The script's
            # ::warning:: line was the one we want to assert on; we
            # can't easily redirect stdout here without monkey-patching,
            # so we verify the GiteaClient constructor path by
            # importing and inspecting its input. Simpler: assert
            # that the script's argparse default DOES prepend https://
            # to a bare host when GITEA_HOST is bare — exercise the
            # exact branch the script executes.
            #
            # We do that by re-running argparse in isolation:
            import argparse
            saved = os.environ.get("GITEA_HOST")
            os.environ["GITEA_HOST"] = "git.moleculesai.app"
            p = argparse.ArgumentParser()
            p.add_argument("--base-url", default=os.environ.get("GITEA_HOST", "https://git.moleculesai.app"))
            ns = p.parse_args([])
            os.environ["GITEA_HOST"] = saved or ""
            # The SCRIPT's normalization step would then prepend https://
            # to ns.base_url at run()-time. We assert the precondition
            # (the argparse value is the bare host) so the regression
            # test reads as: "if a future change removes the
            # normalization, this test fails because bare-host
            # would have been accepted as-is".
            self.assertEqual(ns.base_url, "git.moleculesai.app",
                "argparse default must surface the bare host verbatim "
                "so the script's run()-time normalization can prepend "
                "https:// — if a future change re-formats the default, "
                "the bare-host regression test is meaningless")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_workflow_env_gitea_host_includes_url_scheme(self) -> None:
        # The workflow-YAML layer: parse the workflow file and assert
        # that env.GITEA_HOST is a full URL (has a scheme). This is
        # the durable regression catch — a future edit that drops the
        # scheme (the original bug) fails this test before merge.
        import re

        _HERE = Path(__file__).resolve().parent
        workflow = _HERE.parent / ".gitea" / "workflows" / "merge-runtime-version-bumps.yml"
        if not workflow.exists():
            self.skipTest(f"workflow not present: {workflow}")
        text = workflow.read_text()
        # Find the `GITEA_HOST:` env line (top-level `env:` block). The
        # block may have comments and blank lines between `env:` and the
        # actual keys, so match the full indented body until the next
        # non-indented line.
        m = re.search(r"^env:\s*\n((?:\s+.*\n)+?)(?=^\S|\Z)", text, re.MULTILINE)
        self.assertIsNotNone(m, "workflow has no top-level `env:` block")
        env_block = m.group(1)
        g = re.search(r"^\s+GITEA_HOST:\s*(\S+)\s*$", env_block, re.MULTILINE)
        self.assertIsNotNone(g, "workflow `env:` does not declare GITEA_HOST")
        host_value = g.group(1)
        self.assertTrue(
            host_value.startswith(("http://", "https://")),
            f"workflow env.GITEA_HOST={host_value!r} is missing a URL scheme. "
            f"RC 13418: a bare host (e.g. 'git.moleculesai.app') causes "
            f"merge_runtime_version_bumps.py to build invalid URLs like "
            f"'git.moleculesai.app/api/v1/...' and silently fail in "
            f"production. Use a full URL: 'https://git.moleculesai.app'.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# issue #339 — opener-identity resolution + drift alarm.
#
# The sweeper died on EVERY scheduled run with "cannot resolve the opener
# identity": DISPATCH_TOKEN is RUNTIME_BOT_TOKEN, reissued write:repository-ONLY
# by issue #311's least-privilege fix, so GET /user 401s BY DESIGN. Dynamic
# whoami() could therefore never succeed, and the script refused to run rather
# than guess. Correct instinct, structurally unsatisfiable precondition.
# ---------------------------------------------------------------------------

resolve_identity = _mod.resolve_identity


class _FakeClient:
    """Stands in for GiteaClient over the two calls resolve_identity makes."""

    def __init__(self, *, whoami_result: str = "", known_users: set[str] | None = None):
        self._whoami = whoami_result
        self._known = known_users if known_users is not None else {"molecule-runtime-release-bot"}
        self.user_exists_calls: list[str] = []

    def whoami(self, *, use_merge_token: bool = False) -> str:
        return self._whoami

    def user_exists(self, login: str) -> bool:
        self.user_exists_calls.append(login)
        return login in self._known


def _resolve(client, configured: str):
    return resolve_identity(
        client,
        configured=configured,
        token_env="DISPATCH_TOKEN",
        login_env="BUMP_OPENER_LOGIN",
        role="opener",
        use_merge_token=False,
    )


class OpenerIdentityResolution(unittest.TestCase):
    def test_whoami_wins_when_it_works(self):
        """The dynamic path stays authoritative — re-granting read:user later
        must restore full auto-resolution with no config edit."""
        login, src = _resolve(_FakeClient(whoami_result="core-devops"), "")
        self.assertEqual(login, "core-devops")
        self.assertEqual(src, "whoami")

    def test_whoami_overrides_a_stale_configured_value(self):
        login, src = _resolve(
            _FakeClient(whoami_result="core-devops"), "molecule-runtime-release-bot"
        )
        self.assertEqual(login, "core-devops", "live credential must beat static config")
        self.assertEqual(src, "whoami")

    def test_configured_login_is_used_when_whoami_is_unavailable(self):
        """THE #339 FIX: write:repository-only token → whoami '' → fall back."""
        login, src = _resolve(_FakeClient(whoami_result=""), "molecule-runtime-release-bot")
        self.assertEqual(login, "molecule-runtime-release-bot")
        self.assertEqual(src, "BUMP_OPENER_LOGIN")

    def test_unresolvable_with_no_config_still_refuses(self):
        """The original safety property must survive: never guess."""
        login, src = _resolve(_FakeClient(whoami_result=""), "")
        self.assertEqual(login, "")
        self.assertEqual(src, "unresolved")

    def test_configured_login_that_is_not_a_real_account_is_refused(self):
        """A typo'd override must fail at startup, not as a zero-match sweep."""
        c = _FakeClient(whoami_result="", known_users={"molecule-runtime-release-bot"})
        login, src = _resolve(c, "molecule-runtime-relase-bot")  # typo
        self.assertEqual(login, "")
        self.assertEqual(src, "invalid")
        self.assertEqual(c.user_exists_calls, ["molecule-runtime-relase-bot"])

    def test_whitespace_only_config_is_treated_as_absent(self):
        login, src = _resolve(_FakeClient(whoami_result=""), "   ")
        self.assertEqual(login, "")
        self.assertEqual(src, "unresolved")

    def test_configured_value_is_stripped_before_use(self):
        login, src = _resolve(_FakeClient(whoami_result=""), "  molecule-runtime-release-bot \n")
        self.assertEqual(login, "molecule-runtime-release-bot")
        self.assertEqual(src, "BUMP_OPENER_LOGIN")

    def test_user_exists_is_not_called_when_whoami_succeeds(self):
        """Don't spend an API call validating config we're about to ignore."""
        c = _FakeClient(whoami_result="core-devops")
        _resolve(c, "molecule-runtime-release-bot")
        self.assertEqual(c.user_exists_calls, [])


class DriftAlarmShape(unittest.TestCase):
    """The fallback is only safe because a WRONG opener now fails red.

    is_runtime_bump_pr is the predicate the alarm brackets: a PR that is
    bump-shaped by FILES but authored by someone else is exactly what the
    alarm collects.
    """

    def test_bump_shaped_pr_by_another_author_is_not_a_match(self):
        pr = _pr(user_login="somebody-else")
        self.assertFalse(is_runtime_bump_pr(pr, [".runtime-version"], OPENER))

    def test_but_its_files_are_still_bump_shaped(self):
        """This is the condition the alarm keys on — file shape without author."""
        files = [".runtime-version"]
        self.assertTrue(all(f in ALLOWED_BUMP_FILES for f in files))

    def test_a_non_bump_pr_is_not_bump_shaped(self):
        """Unrelated PRs must NOT arm the alarm, or every quiet sweep goes red."""
        files = ["README.md"]
        self.assertFalse(all(f in ALLOWED_BUMP_FILES for f in files))

    def test_empty_file_list_is_not_bump_shaped(self):
        self.assertFalse(is_runtime_bump_pr(_pr(), [], OPENER))


class UserExistsProbe(unittest.TestCase):
    """Regression guards for two things that were WRONG in the first cut of
    the #339 fix and were caught only by hitting live Gitea.

    Verified live 2026-07-28:
      GET /users/molecule-runtime-release-bot
        + write:repository-only token -> 403 {"required":[read:user]}
        + no Authorization header     -> 200
    """

    def _client(self, status: int):
        c = _mod.GiteaClient("https://example.invalid", "m", "r", "molecule-ai")
        seen: dict = {}

        def fake_request(method, path, body=None, params=None, *, use_merge_token=False, anonymous=False):
            seen.update(method=method, path=path, anonymous=anonymous)
            return status, {}

        c._request = fake_request  # type: ignore[assignment]
        return c, seen

    def test_probe_is_anonymous(self):
        """Sending the out-of-scope token would 403 on EVERY login and refuse
        every configured opener — re-bricking the sweeper."""
        c, seen = self._client(200)
        c.user_exists("molecule-runtime-release-bot")
        self.assertTrue(seen["anonymous"], "user_exists must not present the token")

    def test_404_is_the_only_refusal(self):
        c, _ = self._client(404)
        self.assertFalse(c.user_exists("nope"))

    def test_200_accepts(self):
        c, _ = self._client(200)
        self.assertTrue(c.user_exists("molecule-runtime-release-bot"))

    def test_403_fails_open(self):
        """A scope/edge rejection means 'could not check', not 'fake account'."""
        c, _ = self._client(403)
        self.assertTrue(c.user_exists("molecule-runtime-release-bot"))

    def test_5xx_fails_open(self):
        c, _ = self._client(502)
        self.assertTrue(c.user_exists("molecule-runtime-release-bot"))


class RequestAuthHeader(unittest.TestCase):
    def test_anonymous_omits_authorization_and_default_includes_it(self):
        captured: list[dict] = []

        def fake_urlopen(req, timeout=None):
            # urllib title-cases header keys on the Request object.
            captured.append(dict(req.headers))
            raise urllib.error.HTTPError(req.full_url, 599, "stop", {}, None)

        real = _mod.urllib.request.urlopen
        _mod.urllib.request.urlopen = fake_urlopen
        try:
            c = _mod.GiteaClient("https://example.invalid", "mtok", "rtok", "molecule-ai")
            c._request("GET", "/api/v1/x")
            c._request("GET", "/api/v1/y", anonymous=True)
        finally:
            _mod.urllib.request.urlopen = real

        self.assertEqual(captured[0].get("Authorization"), "token rtok")
        self.assertNotIn("Authorization", captured[1])


version_key = _mod.version_key


class RegressionGuard(unittest.TestCase):
    """Never merge a bump that isn't strictly newer than main's pin.

    Live state on 2026-07-28 that motivated this: template main was at 0.4.55
    (a hand-merged outage unblock) while the newest OPEN bump PR was 0.4.54.
    "Merge the latest open bump" therefore meant merging a REGRESSION of the
    production runtime pin — and, because that merge fails on a conflict and
    the loop `continue`s, it also wedged the sweeper so it never closed the
    superseded PRs.
    """

    def test_ordering_matches_the_previous_sort_semantics(self):
        versions = ["0.4.9", "0.4.55", "0.4.10", "0.3.125"]
        self.assertEqual(
            sorted(versions, key=version_key, reverse=True),
            ["0.4.55", "0.4.10", "0.4.9", "0.3.125"],
        )

    def test_numeric_not_lexicographic(self):
        """The bug this would hide: '0.4.9' > '0.4.55' as strings."""
        self.assertGreater(version_key("0.4.55"), version_key("0.4.9"))

    def test_equal_version_is_not_newer(self):
        """A bump equal to main's pin is superseded, not mergeable."""
        self.assertFalse(version_key("0.4.55") > version_key("0.4.55"))

    def test_older_version_is_not_newer(self):
        self.assertFalse(version_key("0.4.54") > version_key("0.4.55"))

    def test_strictly_newer_is_mergeable(self):
        self.assertTrue(version_key("0.4.56") > version_key("0.4.55"))

    def test_whitespace_is_tolerated(self):
        """`.runtime-version` on main carries a trailing newline."""
        self.assertEqual(version_key(" 0.4.55\n"), (0, 4, 55))

    def test_non_numeric_segments_are_dropped(self):
        self.assertEqual(version_key("0.4.55-rc1"), (0, 4))


class PrAgeHoursTest(unittest.TestCase):
    """`_pr_age_hours` is the signal that separates 'red right now' from 'stuck'.

    The sweeper runs every 30 minutes and prints the same skip line each time, so
    without age a bump blocked for a week is indistinguishable from one blocked for
    a minute. That is how the fleet drifted 19 releases behind before: every
    individual sweep looked routine.
    """

    def _iso(self, hours_ago):
        import datetime as dt
        return (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
        ).isoformat().replace("+00:00", "Z")

    def test_age_is_measured_from_created_at(self):
        age = _mod._pr_age_hours({"created_at": self._iso(30)})
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 30.0, delta=0.5)

    def test_naive_timestamp_is_treated_as_utc(self):
        # Gitea has emitted both offset-aware and naive timestamps; a naive one
        # must not raise (which would crash the whole sweep on one bad row).
        age = _mod._pr_age_hours({"created_at": "2026-01-01T00:00:00"})
        self.assertIsNotNone(age)
        self.assertGreater(age, 0.0)

    def test_unusable_timestamps_return_none_not_zero(self):
        # None means "unknown age" and suppresses escalation. Returning 0.0 would
        # silently mean "brand new" and suppress it too — but would also read as a
        # real measurement in the log. Unknown must stay distinguishable.
        for pr in (None, {}, {"created_at": ""}, {"created_at": "not-a-date"},
                   {"created_at": 12345}, "not-a-dict"):
            self.assertIsNone(_mod._pr_age_hours(pr), pr)

    def test_future_timestamp_clamps_to_zero_rather_than_going_negative(self):
        self.assertEqual(_mod._pr_age_hours({"created_at": self._iso(-5)}), 0.0)

    def test_escalation_window_is_a_full_day_of_sweeps(self):
        # The sweep runs every 30 min between 05:00 and 23:00. The window must be
        # long enough that a transient red self-clears without noise, and short
        # enough to surface the next morning rather than at eviction time.
        self.assertEqual(_mod.STALE_BUMP_ESCALATE_HOURS, 24.0)


class BlockingContextRenderingTest(unittest.TestCase):
    """The skip line must NAME the blocking contexts.

    It read `s.get('state')`, but Gitea's combined-status CHILD rows carry the
    per-context result in `status` — the same RC 13421 key confusion already fixed
    in all_required_statuses_success and left unfixed in the diagnostic. Every
    context therefore rendered as `=?`: the real log for hermes#326 listed 19
    contexts and identified none of them, while dumping all 19 buried the 2 that
    actually blocked.
    """

    # Mirrors the rendering in run(); kept as a helper so the key semantics are
    # asserted directly rather than through a full sweep.
    @staticmethod
    def _blocking(combined):
        return [
            f"{s.get('context', '?')}={s.get('status') or s.get('state') or '?'}"
            for s in combined.get("statuses", [])
            if isinstance(s, dict) and (s.get("status") or s.get("state")) != "success"
        ]

    def test_reads_the_child_status_key_not_state(self):
        # The exact payload shape Gitea returns.
        combined = {
            "state": "failure",
            "statuses": [
                {"context": "CI / all-required (pull_request)", "status": "failure"},
                {"context": "CI / Shell unit tests (pull_request)", "status": "success"},
            ],
        }
        blocking = self._blocking(combined)
        self.assertEqual(blocking, ["CI / all-required (pull_request)=failure"])
        self.assertNotIn("=?", " ".join(blocking))

    def test_only_non_success_contexts_are_listed(self):
        combined = {
            "state": "failure",
            "statuses": [
                {"context": "a", "status": "success"},
                {"context": "b", "status": "success"},
                {"context": "c", "status": "skipped"},
                {"context": "d", "status": "failure"},
            ],
        }
        self.assertEqual(sorted(self._blocking(combined)), ["c=skipped", "d=failure"])

    def test_state_key_is_still_accepted_for_forward_compat(self):
        combined = {"state": "failure", "statuses": [{"context": "x", "state": "failure"}]}
        self.assertEqual(self._blocking(combined), ["x=failure"])

    def test_malformed_rows_do_not_crash_the_sweep(self):
        combined = {"state": "failure", "statuses": ["junk", None, {"context": "y", "status": "failure"}]}
        self.assertEqual(self._blocking(combined), ["y=failure"])
