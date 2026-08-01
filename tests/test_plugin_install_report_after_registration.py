"""The plugin-install report must be SENT after registration mints the token.

runtime#390. ``POST /workspaces/:id/plugin-install-report`` is registered under
``wsAuth`` — ``middleware.WorkspaceAuth`` — in molecule-core
(``workspace-server/internal/router/router.go:581``), and the vendored SDK
contract says so itself::

    "auth": "Authorization: Bearer <workspace scoped token>"

That bearer does not exist until ``register_with_platform`` receives it from the
first successful ``/registry/register`` and ``platform_auth.save_token`` writes
``<configs>/.auth_token``. The reporter used to fire ~620 lines EARLIER, inline
with ``install_declared_plugins()``, so on a first boot ``auth_headers()``
returned an empty dict, the POST went out with no ``Authorization`` header, core
refused it 401, fail-soft swallowed the 401 — and no row was ever written for any
workspace on its first boot. Live: workspace 4b9771e5 on staging ``gm360repro``,
runtime 0.4.71, booted and serving, ``GET .../plugin-install-report`` → 404
"never reported".

WHY THE EXISTING SUITE COULD NOT CATCH THIS
-------------------------------------------

``tests/test_plugin_install_report.py`` covers the producer thoroughly, and every
one of its arms is true both before and after the bug:

* its ``_env`` fixture sets ``MOLECULE_WORKSPACE_TOKEN``, so ``auth_headers()``
  ALWAYS returns a bearer there — the token-less first boot is never exercised;
* the drop arms assert "returns False and does not raise", which is exactly what
  a 401 does. A permanently-401'd reporter passes that suite forever.

So the property under test here is not "does a POST happen" and not "is a failure
swallowed" — both were already true. It is **is the POST made at a point in boot
where it can be authenticated at all**.

``main()`` is ``# pragma: no cover`` (the whole boot orchestration) and is not
drivable in a unit test, so the ordering itself is pinned by AST inspection of the
real source — the established convention in this repo for main()'s monolith
wiring (see ``test_load_config_opt_fallback``'s config_path ordering gate and
``test_main_poll_mode``'s initial-prompt gate). The last test then takes the order
it finds IN THE SOURCE and runs it against a stand-in that enforces core's actual
wsAuth rule, so the structural fact is cashed out as the observable 401-vs-204 the
live tenant showed.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field

import httpx
import pytest

import molecule_runtime.main as main_mod
from molecule_runtime import plugin_install_report as pir


# Both spellings, so this file is meaningful against the PRE-fix source too: on
# `main` the send site is a bare `report_install_outcome(...)` inside the
# boot-install block; after the fix it is the `_send_plugin_install_report(...)`
# wrapper. Either one is "the send", and there must be exactly one of them.
_SEND_CALL_NAMES = {"report_install_outcome", "_send_plugin_install_report"}
_REGISTER_CALL_NAME = "register_with_platform"


def _main_tree() -> ast.AST:
    return ast.parse(inspect.getsource(main_mod.main))


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _call_linenos(names: set[str]) -> list[int]:
    """Line numbers (relative to main()'s source) of calls to any of *names*."""
    return sorted(
        node.lineno
        for node in ast.walk(_main_tree())
        if isinstance(node, ast.Call) and _called_name(node) in names
    )


# ---------------------------------------------------------------------------
# 1. The ordering itself — the bug.
# ---------------------------------------------------------------------------


def test_the_report_is_sent_after_registration_not_before():
    """The whole defect in one assertion.

    Before the fix these are line 687 (send) and line 1309 (register) of
    main.py: the reporter runs a full boot phase BEFORE the call that mints the
    credential its receiver requires.
    """
    send = _call_linenos(_SEND_CALL_NAMES)
    register = _call_linenos({_REGISTER_CALL_NAME})

    assert send, "main() no longer sends a plugin-install report at all"
    assert register, "main() no longer calls register_with_platform"

    assert min(send) > max(register), (
        "the plugin-install report is sent BEFORE register_with_platform "
        f"(send at main()+{min(send)}, register at main()+{max(register)}). "
        "register_with_platform is what mints and saves this workspace's "
        "auth token; core registers the report route under wsAuth "
        "(router.go:581), so a report sent before it carries no Authorization "
        "header and is 401'd — silently, on every first boot. runtime#390."
    )


def test_there_is_exactly_one_send_site_in_main():
    """One POST per boot is a contract property, not an accident.

    ``plugin_install_report``'s docstring leans on it ("There is at most ONE
    such line per boot") to argue the module does not need the concierge gate
    that blinded boot_step_emit. A fix that re-sent the report after
    registration while LEAVING the pre-registration call in place would satisfy
    the ordering test above and quietly double the volume.
    """
    send = _call_linenos(_SEND_CALL_NAMES)
    assert len(send) == 1, (
        f"expected exactly one plugin-install-report send site in main(), found "
        f"{len(send)} at main()+{send}"
    )


def test_the_report_object_is_bound_before_the_boot_install_can_raise():
    """The send site must be reachable on the boot-install-failed path too.

    ``_plugin_install_report`` is assigned inside a ``try`` whose ``except``
    exists precisely because ``install_declared_plugins()`` can blow up. Moving
    the send out of that ``try`` means the name is read hundreds of lines later
    on a path where it may never have been assigned — an unbound local raising
    NameError straight into the boot path, i.e. observability becoming a boot
    dependency, which is the one thing this module must never do.
    """
    tree = _main_tree()
    init_linenos = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_plugin_install_report"
            for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is None
    ]
    install_linenos = _call_linenos({"install_declared_plugins"})

    assert init_linenos, (
        "main() never binds `_plugin_install_report = None` before the "
        "boot-install try block, so a raising install_declared_plugins() leaves "
        "the name unbound for the reporting site"
    )
    assert install_linenos, "main() no longer calls install_declared_plugins()"
    assert min(init_linenos) < min(install_linenos), (
        "`_plugin_install_report = None` must precede the "
        "install_declared_plugins() call it guards"
    )


# ---------------------------------------------------------------------------
# 2. The send helper — fail-soft, exactly once, unmutated report.
# ---------------------------------------------------------------------------


@dataclass
class _Report:
    """InstallReport's attribute surface. Deliberately not the real dataclass —
    a failure here must be a reporting failure, not a construction failure."""

    declared: bool = True
    plugins_dir: str = "/configs/plugins"
    installed: list[str] = field(default_factory=lambda: ["gitea://o/r#v1"])
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    swapped: bool = True
    installed_refs: dict = field(default_factory=lambda: {"gitea://o/r#v1": "a" * 40})


def test_send_helper_is_a_noop_when_boot_install_produced_no_report(monkeypatch):
    """No report object => nothing is sent. NOT an empty report.

    Synthesising one would POST a definitive ``declared:false`` — "core never
    asked for a plugin" — which core accepts and persists, and which is the
    exact mis-diagnosis ``report_payload``'s absent-is-not-false rule exists to
    prevent.
    """
    calls: list[object] = []
    monkeypatch.setattr(pir, "report_install_outcome", lambda r: calls.append(r))

    assert main_mod._send_plugin_install_report(None) is False
    assert calls == [], "a missing boot-install report must not be invented"


def test_send_helper_forwards_the_exact_report_object_once(monkeypatch):
    """The report must describe the boot-install that actually happened.

    The measurement is taken at step 0.2c and handed over unchanged; the send
    site must not re-derive it against a tree that later steps have touched.
    """
    seen: list[object] = []

    def _capture(report):
        seen.append(report)
        return True

    monkeypatch.setattr(pir, "report_install_outcome", _capture)

    rep = _Report()
    assert main_mod._send_plugin_install_report(rep) is True
    assert len(seen) == 1, "exactly one POST per boot"
    assert seen[0] is rep, "the send site must forward the boot-install's own report"


def test_send_helper_never_raises_into_boot(monkeypatch):
    """Fail-soft is the property that made this bug survivable; it must survive
    the fix. ``report_install_outcome`` does not raise today, but the send now
    happens at a much later boot site where a raise would abort a workspace that
    is otherwise ready to serve."""

    def _boom(report):
        raise RuntimeError("platform exploded")

    monkeypatch.setattr(pir, "report_install_outcome", _boom)
    assert main_mod._send_plugin_install_report(_Report()) is False


def test_boot_does_not_branch_on_the_report_result():
    """Observability must never become a boot dependency. The call site is a
    bare expression statement — not an ``if``, not an ``assert``, not assigned
    to something a later step reads."""
    bare_statements = [
        node.lineno
        for node in ast.walk(_main_tree())
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and _called_name(node.value) in _SEND_CALL_NAMES
    ]
    assert bare_statements == _call_linenos(_SEND_CALL_NAMES), (
        "the plugin-install-report send is not a bare expression statement — "
        "boot must never branch on whether telemetry was accepted"
    )


# ---------------------------------------------------------------------------
# 3. The consequence, against core's real auth rule.
# ---------------------------------------------------------------------------


class _WsAuthPlatform:
    """A stand-in for core's wsAuth group, reduced to the one rule that matters.

    ``middleware.WorkspaceAuth`` (wsauth_middleware.go:67) aborts 401 when there
    is no bearer and no verified CP session; with a valid bearer the report
    handler answers 204 (the contract's ``success_status``). No grandfathering —
    that belongs to the heartbeat handler, which is a different route.
    """

    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        authorized = bool(headers.get("Authorization"))
        self.requests.append({"url": url, "authorized": authorized})
        return httpx.Response(204 if authorized else 401)


@pytest.fixture
def first_boot(monkeypatch, tmp_path):
    """A genuine first boot: a configs dir with no ``.auth_token`` in it, and no
    token in the environment either."""
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("PLATFORM_URL", "http://platform.test:8080")
    monkeypatch.setenv("WORKSPACE_ID", "4b9771e5-d5ff-4a69-b713-fd9b44c56961")
    monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)

    from molecule_runtime import platform_auth

    monkeypatch.setattr(platform_auth, "_cached_token", None, raising=False)
    monkeypatch.setattr(platform_auth, "_validated_workspace_id", None, raising=False)
    yield platform_auth
    platform_auth._cached_token = None
    platform_auth._validated_workspace_id = None


def test_a_first_boot_has_no_bearer_until_registration_saves_one(first_boot):
    """The precondition, stated plainly: this is why the old ordering could not
    work, and it is not a misconfiguration — the credential does not exist yet."""
    assert "Authorization" not in first_boot.auth_headers()
    first_boot.save_token("ws-token-from-registry-register")
    assert first_boot.auth_headers()["Authorization"] == (
        "Bearer ws-token-from-registry-register"
    )


def test_mains_declared_boot_order_gets_the_report_past_wsauth(
    first_boot, monkeypatch
):
    """Run the two boot steps IN THE ORDER main.py declares them, on a first
    boot, against core's real wsAuth rule.

    This is the ordering test cashed out as behaviour. Registration is stood in
    for by its one relevant side effect — ``save_token``, main.py:336 — because
    that is the only thing about registration the report depends on.

    Against the pre-fix source the AST says report-then-register, the report goes
    out with no Authorization header, and this fails with the live 401. Against
    the fixed source it is register-then-report, the bearer is present, and core
    accepts it 204 — which is what turns the live
    ``GET /workspaces/<id>/plugin-install-report`` from 404 into a real report.
    """
    platform = _WsAuthPlatform()
    monkeypatch.setattr(pir.httpx, "request", platform)

    send = min(_call_linenos(_SEND_CALL_NAMES))
    register = max(_call_linenos({_REGISTER_CALL_NAME}))

    def _do_register():
        first_boot.save_token("ws-token-from-registry-register")

    def _do_report():
        return pir.report_install_outcome(_Report())

    steps = (
        [_do_register, _do_report] if send > register else [_do_report, _do_register]
    )
    results = [step() for step in steps]
    accepted = any(r is True for r in results)

    assert len(platform.requests) == 1, "exactly one POST per boot"
    assert platform.requests[0]["authorized"], (
        "the plugin-install report reached core's wsAuth route with NO "
        "Authorization header — this is the live 401 from runtime#390: the "
        "report is sent before register_with_platform has minted the token"
    )
    assert accepted, (
        "core refused the report (401). Nothing is recorded for this workspace "
        "and GET /workspaces/<id>/plugin-install-report stays 404 forever"
    )
