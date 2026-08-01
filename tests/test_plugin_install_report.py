"""Contract for the plugin boot-install report (producer side).

The property under test is NOT "does an HTTP POST happen". It is "can the platform
find out that a workspace booted with no live plugins" — because before this module
it could not, for any ordinary workspace, and molecule-core#4953 spent three
proposed-and-retracted explanations on a symptom whose cause was unobservable.

So the tests concentrate on the five ways this could look like it works and not:

 1. **gating** — if this ever becomes concierge-gated it recreates the exact blind
    spot it was written to close, and the change would look like noise reduction
 2. **fail-soft** — if a report failure can raise, observability has become a boot
    dependency and a network blip stops a workspace booting
 3. **the payload** — if the wire shape is hand-written rather than driven by the
    contract, a rename silently sends a field the receiver ignores
 4. **`swapped`** — the field that distinguishes "staged" from "live"; dropping it
    turns a total failure into an apparent success
 5. **absent vs false** — an attribute the report object does not HAVE must never
    be projected as a definitive `false`. Core made `declared`/`swapped` `*bool`
    precisely so ABSENT can be refused with 400; a producer that coerces defeats
    that and persists "core never asked for a plugin" — the wrong repo, and the
    exact mis-diagnosis the field exists to prevent

``httpx.request`` is monkeypatched rather than the client class: the module calls
the module-level function, which is the honest seam.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from molecule_runtime import plugin_install_report as pir

# The two wire fields core declares as *bool (pointers) so that ABSENT is
# distinguishable from false, and refuses with 400 when either is nil:
# molecule-core workspace-server/internal/handlers/plugin_install_report.go
#   if body.Declared == nil || body.Swapped == nil { 400 }
# Anything this producer omits from those two is a LOUD, visible refusal; anything
# it invents is a silent, persisted lie.
_CORE_REQUIRED_POINTER_FIELDS = ("declared", "swapped")


# A stand-in with the same attribute surface as
# molecule_runtime.plugin_sources.InstallReport. Deliberately NOT importing the
# real dataclass: this file is about the projection contract, and a local stub
# keeps a failure here from being an InstallReport-construction failure.
@dataclass
class _Report:
    declared: bool = False
    plugins_dir: str | None = None
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    swapped: bool = False
    # core#5007: declared source -> the commit the box actually installed.
    installed_refs: dict = field(default_factory=dict)


def _report_missing(*attrs: str) -> object:
    """An InstallReport-shaped object with ``attrs`` REMOVED.

    This is the drift shape, and it is not hypothetical: a field renamed on the
    dataclass alone, or ``install_declared_plugins`` returning some other type,
    produces exactly this. Everything present is deliberately TRUTHY so a coerced
    ``false`` cannot be mistaken for the real value.
    """
    full: dict[str, object] = {
        "declared": True,
        "plugins_dir": "/configs/plugins",
        "installed": ["gitea://o/r#v1"],
        "skipped": ["local-thing"],
        "installed_refs": {"gitea://o/r#v1": "a"*40},
        "failed": [],
        "swapped": True,
    }
    for attr in attrs:
        del full[attr]
    return SimpleNamespace(**full)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PLATFORM_URL", "http://platform.test:8080")
    monkeypatch.setenv("WORKSPACE_ID", "6ac59acb-a79d-4686-8669-c5a2c077d69d")
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "tok-xyz")


class _Recorder:
    def __init__(self, response: httpx.Response | None = None, raises: Exception | None = None):
        self.calls: list[dict] = []
        self._response = response
        self._raises = raises

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response or httpx.Response(204)


def _install(monkeypatch, rec: _Recorder):
    monkeypatch.setattr(pir.httpx, "request", rec)
    return rec


# --- 1. gating: the whole point ---------------------------------------------


def test_is_not_concierge_gated():
    assert pir.concierge_gated() is False, (
        "gating this on kind=platform is exactly what made a fleet-wide "
        "boot-install failure invisible; the contract pins it false"
    )


def test_platform_persists_it():
    assert pir.durable() is True


def test_an_ordinary_workspace_reports(monkeypatch):
    """The regression test for the blind spot: no platform-agent marker anywhere in
    the env, and the report still goes out."""
    monkeypatch.delenv("MOLECULE_PLATFORM_AGENT_IMAGE", raising=False)
    rec = _install(monkeypatch, _Recorder())
    assert pir.report_install_outcome(_Report(declared=True, swapped=True)) is True
    assert len(rec.calls) == 1, "an ordinary (non-concierge) workspace MUST report"


# --- 2. fail-soft: never break a boot ---------------------------------------


def test_transport_error_is_swallowed(monkeypatch):
    _install(monkeypatch, _Recorder(raises=httpx.ConnectError("no route")))
    assert pir.report_install_outcome(_Report(declared=True)) is False


def test_timeout_is_swallowed(monkeypatch):
    _install(monkeypatch, _Recorder(raises=httpx.ReadTimeout("slow")))
    assert pir.report_install_outcome(_Report(declared=True)) is False


def test_404_from_an_older_platform_is_swallowed(monkeypatch):
    """The runtime must be shippable BEFORE core's handler exists."""
    _install(monkeypatch, _Recorder(response=httpx.Response(404)))
    assert pir.report_install_outcome(_Report(declared=True)) is False


def test_500_is_swallowed(monkeypatch):
    _install(monkeypatch, _Recorder(response=httpx.Response(500)))
    assert pir.report_install_outcome(_Report(declared=True)) is False


def test_a_broken_report_object_does_not_raise(monkeypatch):
    """Even a garbage input must not raise into boot.

    This test used to stop there, and in stopping there it RATIFIED the bug: the
    old projection turned ``object()`` into a confident
    ``{'declared': False, ..., 'swapped': False}``, core accepted it with 204 and
    persisted it, and the test called that a pass. So the no-raise assertion stays
    — it is the property that matters on the boot path — but it is now paired with
    the check that nothing was FABRICATED. See
    ``test_a_foreign_object_projects_to_an_empty_payload_not_a_confident_false``
    for the payload half stated directly.
    """
    rec = _install(monkeypatch, _Recorder())
    assert pir.report_install_outcome(object()) in (True, False)  # type: ignore[arg-type]
    for call in rec.calls:
        body = call["json"]
        for wire in _CORE_REQUIRED_POINTER_FIELDS:
            assert wire not in body, (
                f"{wire}={body[wire]!r} was invented for an object that has no such "
                "attribute — core would accept and persist it, and an operator would "
                "read it as a definitive answer"
            )


def test_missing_platform_url_reports_nothing_without_erroring(monkeypatch):
    monkeypatch.delenv("PLATFORM_URL", raising=False)
    monkeypatch.setenv("WORKSPACE_ID", "")
    rec = _install(monkeypatch, _Recorder())
    assert pir.report_install_outcome(_Report(declared=True)) is False
    assert rec.calls == [], "must not fire a request with no workspace id"


def test_malformed_workspace_id_is_not_interpolated(monkeypatch):
    """The id lands in a URL PATH, so a malformed one must stop the request rather
    than be pasted in (the CWE-20 gate boot_step_emit routes through)."""
    monkeypatch.setenv("WORKSPACE_ID", "../../etc/passwd")
    rec = _install(monkeypatch, _Recorder())
    pir.report_install_outcome(_Report(declared=True))
    for call in rec.calls:
        assert ".." not in call["url"], f"path traversal reached the URL: {call['url']}"


# --- 2b. fail-soft is not the same as SILENT --------------------------------
#
# The arms below all already returned False and already did not raise, and the
# tests above proved exactly that — which is why they passed just as happily when
# every drop was invisible. A workspace that never reported was indistinguishable
# from one that reported fine, and that is the same blind spot this module was
# written to close, one level up.
#
# So these tests pin the LEVEL and the CONTENT of each arm, and are paired with
# negative controls (a 2xx and a 404 must NOT warn) so "warn about everything"
# cannot pass either.


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _run_and_capture(caplog, monkeypatch, rec, report=None):
    """Drive one report_install_outcome and return its WARNING lines."""
    _install(monkeypatch, rec)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=pir.__name__):
        result = pir.report_install_outcome(report or _Report(declared=True, swapped=True))
    return result, _warnings(caplog)


# The actionable arms: each must produce exactly one WARNING that names WHY.
_ACTIONABLE_ARMS = [
    pytest.param(
        lambda: _Recorder(raises=httpx.ConnectError("no route to host")),
        ["could not reach", "ConnectError"],
        id="transport-error",
    ),
    pytest.param(
        lambda: _Recorder(raises=httpx.ReadTimeout("slow")),
        ["could not reach", "ReadTimeout"],
        id="timeout",
    ),
    pytest.param(
        lambda: _Recorder(response=httpx.Response(401)),
        ["401", "credentials"],
        id="401-unauthorized",
    ),
    pytest.param(
        lambda: _Recorder(response=httpx.Response(403)),
        ["403", "credentials"],
        id="403-forbidden",
    ),
    pytest.param(
        lambda: _Recorder(response=httpx.Response(500)),
        ["500"],
        id="500-server-error",
    ),
    pytest.param(
        lambda: _Recorder(response=httpx.Response(502)),
        ["502"],
        id="502-bad-gateway",
    ),
]


@pytest.mark.parametrize("make_rec,expected_fragments", _ACTIONABLE_ARMS)
def test_an_actionable_drop_warns(caplog, monkeypatch, make_rec, expected_fragments):
    """Each actionable drop arm must be visible at WARNING, not whispered at DEBUG."""
    result, warns = _run_and_capture(caplog, monkeypatch, make_rec())
    assert result is False
    assert len(warns) == 1, f"expected exactly one WARNING per boot, got {warns}"
    for fragment in expected_fragments:
        assert fragment in warns[0], (
            f"the warning must say WHY it dropped; {fragment!r} missing from {warns[0]!r}"
        )


@pytest.mark.parametrize("make_rec,_frags", _ACTIONABLE_ARMS)
def test_an_actionable_drop_names_the_resolved_url(caplog, monkeypatch, make_rec, _frags):
    """"It dropped" is not actionable; "it dropped talking to X" is.

    The resolved URL is the one thing that separates "pointed at the wrong
    platform" from "pointed at the right platform and refused".
    """
    _result, warns = _run_and_capture(caplog, monkeypatch, make_rec())
    assert "http://platform.test:8080" in warns[0], (
        f"the resolved platform URL must appear in the warning: {warns[0]!r}"
    )


def test_a_401_is_not_reported_as_a_version_skew(caplog, monkeypatch):
    """The distinction that makes the 404 carve-out safe.

    A 404 is a rollout artefact that fixes itself; a 401 never does. If the
    warning blurs them, an operator waits for a deploy that will not help.
    """
    _result, warns = _run_and_capture(
        caplog, monkeypatch, _Recorder(response=httpx.Response(401))
    )
    assert "not a platform-version skew" in warns[0]


# --- negative controls: the arms that must STAY quiet ------------------------


def test_404_from_an_older_platform_does_not_warn(caplog, monkeypatch):
    """NEGATIVE CONTROL for the whole section.

    A platform that predates core's handler answers 404 by construction. During a
    rollout that is correct behaviour, it is not actionable from inside the box,
    and warning about it trains an operator to ignore this logger — which would
    cost exactly the visibility the rest of this section buys.
    """
    result, warns = _run_and_capture(
        caplog, monkeypatch, _Recorder(response=httpx.Response(404))
    )
    assert result is False
    assert warns == [], f"a 404 must not warn, got {warns}"


def test_404_is_still_logged_somewhere(caplog, monkeypatch):
    """Quiet-ish, not silent: it must still be greppable at DEBUG, with the URL."""
    _install(monkeypatch, _Recorder(response=httpx.Response(404)))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=pir.__name__):
        pir.report_install_outcome(_Report(declared=True, swapped=True))
    assert "404" in caplog.text
    assert "http://platform.test:8080" in caplog.text


def test_an_accepted_report_does_not_warn(caplog, monkeypatch):
    """NEGATIVE CONTROL: the happy path must be silent at WARNING.

    Without this, "log every arm at warning" passes the whole section and every
    healthy boot emits a warning.
    """
    result, warns = _run_and_capture(caplog, monkeypatch, _Recorder(httpx.Response(204)))
    assert result is True
    assert warns == [], f"an accepted report must not warn, got {warns}"


@pytest.mark.parametrize("code", [200, 201, 202, 204])
def test_no_2xx_warns(caplog, monkeypatch, code):
    _result, warns = _run_and_capture(caplog, monkeypatch, _Recorder(httpx.Response(code)))
    assert warns == [], f"{code} must not warn, got {warns}"


# --- the resolution arm ------------------------------------------------------


def test_an_unresolvable_workspace_id_warns(caplog, monkeypatch):
    """The arm that fires on a misprovisioned box, and the likeliest real cause of
    a silent non-reporter. At debug it looked exactly like a healthy boot."""
    monkeypatch.setenv("WORKSPACE_ID", "")
    result, warns = _run_and_capture(caplog, monkeypatch, _Recorder())
    assert result is False
    assert len(warns) == 1, f"expected exactly one WARNING, got {warns}"
    assert "workspace id" in warns[0]
    assert "WORKSPACE_ID" in warns[0], "the warning must name the env var to fix"


def test_a_malformed_workspace_id_warns_rather_than_vanishing(caplog, monkeypatch):
    """A WORKSPACE_ID that fails the CWE-20 gate resolves to "" and drops. Refusing
    to interpolate it is right; doing so silently is what made it invisible."""
    monkeypatch.setenv("WORKSPACE_ID", "../../etc/passwd")
    result, warns = _run_and_capture(caplog, monkeypatch, _Recorder())
    assert result is False
    assert len(warns) == 1, f"expected exactly one WARNING, got {warns}"
    assert "workspace id" in warns[0]


def test_the_resolution_warning_does_not_claim_the_url_was_reached(caplog, monkeypatch):
    """It dropped BEFORE sending. Saying otherwise sends an operator to the
    receiver's logs to look for a request that was never made."""
    monkeypatch.setenv("WORKSPACE_ID", "")
    _result, warns = _run_and_capture(caplog, monkeypatch, _Recorder())
    assert "before sending" in warns[0]


def test_nothing_is_sent_when_resolution_fails(monkeypatch):
    """Raising the log level must not have turned a no-op into a request."""
    monkeypatch.setenv("WORKSPACE_ID", "")
    rec = _install(monkeypatch, _Recorder())
    pir.report_install_outcome(_Report(declared=True))
    assert rec.calls == []


# --- the properties raising the level must NOT have changed ------------------


_ALL_DROP_ARMS = [
    pytest.param(lambda: _Recorder(raises=httpx.ConnectError("x")), id="transport"),
    pytest.param(lambda: _Recorder(raises=httpx.ReadTimeout("x")), id="timeout"),
    pytest.param(lambda: _Recorder(raises=RuntimeError("unexpected")), id="unexpected-exception"),
    pytest.param(lambda: _Recorder(response=httpx.Response(400)), id="400"),
    pytest.param(lambda: _Recorder(response=httpx.Response(401)), id="401"),
    pytest.param(lambda: _Recorder(response=httpx.Response(403)), id="403"),
    pytest.param(lambda: _Recorder(response=httpx.Response(404)), id="404"),
    pytest.param(lambda: _Recorder(response=httpx.Response(500)), id="500"),
]


@pytest.mark.parametrize("make_rec", _ALL_DROP_ARMS)
def test_every_drop_arm_is_still_fail_soft(monkeypatch, make_rec):
    """The invariant the whole module rests on, restated across every arm.

    A log level cannot change control flow — but this is cheap, and the boot path
    calls this function without branching on it, so "returns a bool and raises
    nothing" is the property that keeps a network blip from stopping a boot.
    """
    _install(monkeypatch, make_rec())
    assert pir.report_install_outcome(_Report(declared=True, swapped=True)) is False


@pytest.mark.parametrize("make_rec", _ALL_DROP_ARMS)
def test_no_drop_arm_logs_more_than_one_line_per_boot(caplog, monkeypatch, make_rec):
    """This runs ONCE per boot, after install_declared_plugins — not once per
    phase. One line is a signal; a burst is noise an operator filters out."""
    _result, warns = _run_and_capture(caplog, monkeypatch, make_rec())
    assert len(warns) <= 1, f"more than one warning for a single boot: {warns}"


@pytest.mark.parametrize("make_rec", _ALL_DROP_ARMS)
def test_no_drop_arm_leaks_the_workspace_token(caplog, monkeypatch, make_rec):
    """The resolved URL is safe to log; the Authorization header is not."""
    _install(monkeypatch, make_rec())
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=pir.__name__):
        pir.report_install_outcome(_Report(declared=True, swapped=True))
    assert "tok-xyz" not in caplog.text, "the workspace token reached the log"


def test_an_unexpected_exception_still_warns_with_its_type(caplog, monkeypatch):
    """Not just httpx errors — a bug in report_payload or platform_auth lands here
    too, and naming the exception type is what makes it triageable."""
    _result, warns = _run_and_capture(
        caplog, monkeypatch, _Recorder(raises=RuntimeError("auth_headers blew up"))
    )
    assert len(warns) == 1
    assert "RuntimeError" in warns[0]
    assert "auth_headers blew up" in warns[0]


def test_a_pre_url_failure_says_the_url_was_unresolved(caplog, monkeypatch):
    """An exception raised before the URL exists must not print a half-built one."""
    def _boom(*_a, **_k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(pir, "path_template", _boom)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=pir.__name__):
        assert pir.report_install_outcome(_Report(declared=True)) is False
    warns = _warnings(caplog)
    assert len(warns) == 1
    assert "unresolved" in warns[0]


# --- 3. the payload is contract-driven --------------------------------------


def test_payload_uses_the_contract_field_names():
    payload = pir.report_payload(
        _Report(declared=True, plugins_dir="/configs/plugins", installed=["a"], swapped=True)
    )
    assert set(payload) == set(pir.field_names().values())


def test_payload_carries_every_field(monkeypatch):
    rec = _install(monkeypatch, _Recorder())
    rep = _Report(
        declared=True,
        plugins_dir="/configs/plugins",
        installed=["gitea://o/r#v1"],
        skipped=["local-thing"],
        failed=[],
        swapped=True,
    )
    assert pir.report_install_outcome(rep) is True
    body = rec.calls[0]["json"]
    assert body["declared"] is True
    assert body["swapped"] is True
    assert body["plugins_dir"] == "/configs/plugins"
    assert body["installed"] == ["gitea://o/r#v1"]
    assert body["skipped"] == ["local-thing"]
    assert body["failed"] == []


def test_none_lists_become_empty_lists_not_null():
    """A receiver must not have to tell "no failures" from "nothing said about
    failures"; core stores [] rather than null for the same reason."""
    rep = _Report(declared=False)
    rep.installed = None  # type: ignore[assignment]
    rep.skipped = None  # type: ignore[assignment]
    rep.failed = None  # type: ignore[assignment]
    payload = pir.report_payload(rep)
    for key in ("installed", "skipped", "failed"):
        assert payload[key] == [], f"{key} must serialise as [], got {payload[key]!r}"
    assert json.dumps(payload)  # must be JSON-encodable


def test_none_plugins_dir_becomes_empty_string():
    assert pir.report_payload(_Report(declared=False)).get("plugins_dir") == ""


def test_url_is_built_from_the_contract_template(monkeypatch):
    rec = _install(monkeypatch, _Recorder())
    pir.report_install_outcome(_Report(declared=True, swapped=True))
    url = rec.calls[0]["url"]
    assert url.endswith(
        "/workspaces/6ac59acb-a79d-4686-8669-c5a2c077d69d/plugin-install-report"
    ), url
    assert "{workspace_id}" not in url, "the template placeholder must be substituted"
    assert rec.calls[0]["method"] == pir.http_method()


def test_auth_header_is_present(monkeypatch):
    rec = _install(monkeypatch, _Recorder())
    pir.report_install_outcome(_Report(declared=True, swapped=True))
    headers = rec.calls[0]["headers"]
    assert any(k.lower() == "authorization" for k in headers), (
        "the POST is wsAuth-mounted; without a bearer the platform refuses it"
    )


# --- 4. swapped is the load-bearing field ----------------------------------


def test_swapped_false_is_transmitted_as_false(monkeypatch):
    """THE production shape: everything staged, nothing promoted. If `swapped` were
    dropped or defaulted true, a total failure would arrive looking like a success —
    which is precisely the state that was indistinguishable before this contract."""
    rec = _install(monkeypatch, _Recorder())
    rep = _Report(declared=True, installed=["a", "b", "c", "d", "e", "f"], swapped=False)
    pir.report_install_outcome(rep)
    body = rec.calls[0]["json"]
    assert body["swapped"] is False
    assert len(body["installed"]) == 6, (
        "the staged list must still be sent — it is the diagnostic that says HOW "
        "far the install got"
    )


def test_declared_false_is_transmitted_as_false(monkeypatch):
    """Separates "core never asked for a plugin" from "core asked and nothing
    landed" — different bugs, different repos."""
    rec = _install(monkeypatch, _Recorder())
    pir.report_install_outcome(_Report(declared=False))
    assert rec.calls[0]["json"]["declared"] is False


# --- 5. absent is not false -------------------------------------------------
#
# Core: `Declared *bool` / `Swapped *bool`, and
#   if body.Declared == nil || body.Swapped == nil {
#       400 "declared and swapped are required (absent is not the same as false)"
#   }
# The pointers only buy anything if the PRODUCER refuses to fill them in. These
# tests are the producer half of that contract.


def test_the_required_pointer_fields_are_real_contract_fields():
    """Guard for the parametrized tests below: they delete by ATTRIBUTE name and
    assert on WIRE name, which is only sound while the contract maps them 1:1. If
    a rename breaks that, these tests must fail here rather than quietly assert
    nothing about a key that never existed."""
    fields = pir.field_names()
    for name in _CORE_REQUIRED_POINTER_FIELDS:
        assert fields.get(name) == name, (
            f"{name!r} is no longer an identity attr->wire mapping ({fields.get(name)!r}); "
            "the absent-vs-false tests need updating, not deleting"
        )


@pytest.mark.parametrize("gone", _CORE_REQUIRED_POINTER_FIELDS)
def test_an_absent_required_field_is_omitted_not_coerced_to_false(gone):
    """The core finding: a missing attribute must leave a HOLE, not a `false`.

    A hole makes core answer 400 with the reason in the body — visible in core's
    request log, which is readable where this box's stdout is not. A `false` is
    accepted, persisted, and read by an operator as a definitive statement about
    the wrong repo.
    """
    payload = pir.report_payload(_report_missing(gone))  # type: ignore[arg-type]
    assert gone not in payload, (
        f"{gone!r} is absent on the report object but was still projected as "
        f"{payload.get(gone)!r} — that is the confident lie core's *bool exists to "
        "refuse"
    )


@pytest.mark.parametrize("gone", _CORE_REQUIRED_POINTER_FIELDS)
def test_omitting_one_field_does_not_drop_the_others(gone):
    """The report is still worth sending: only the drifted field is withheld, so
    core's 400 names a specific hole rather than arriving as an empty body."""
    payload = pir.report_payload(_report_missing(gone))  # type: ignore[arg-type]
    assert set(payload) == set(pir.field_names().values()) - {gone}


def test_a_foreign_object_projects_to_an_empty_payload_not_a_confident_false():
    """``object()`` has none of the attributes, so it must project to NOTHING.

    Before the fix this returned
    ``{'declared': False, 'plugins_dir': '', 'installed': [], 'skipped': [],
       'failed': [], 'swapped': False}`` — a complete, well-formed, entirely
    fabricated report that core accepts with 204.
    """
    assert pir.report_payload(object()) == {}  # type: ignore[arg-type]


def test_present_and_false_is_still_sent_as_false():
    """The boundary. Absent must be omitted; PRESENT-and-false must still be sent,
    because `declared=false` / `swapped=false` are real, load-bearing answers —
    dropping them would trade one blind spot for another."""
    payload = pir.report_payload(_Report(declared=False, swapped=False))
    assert payload["declared"] is False
    assert payload["swapped"] is False


def test_present_and_none_is_still_coerced_not_omitted():
    """`plugins_dir: str | None` is legitimately None on a report that installed
    nothing. Present-but-None is a value, not drift, so it is normalised (to "")
    rather than omitted."""
    payload = pir.report_payload(_Report(declared=True, plugins_dir=None, swapped=True))
    assert payload["plugins_dir"] == ""


def test_the_omission_is_logged_loudly(caplog):
    """Belt to the 400's braces. Not the primary signal — this module exists
    BECAUSE stdout is not readable on a locked-down prod box — but a warning costs
    nothing and is the one place a local reader can see the drift."""
    with caplog.at_level(logging.WARNING, logger=pir.__name__):
        pir.report_payload(_report_missing("declared"))  # type: ignore[arg-type]
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "silently omitting is better than silently lying, but it should not be silent"
    )
    assert "declared" in caplog.text


def test_drifted_report_still_does_not_break_the_boot(monkeypatch, caplog):
    """Fail-soft is NOT weakened by making the payload honest.

    Core answers 400 to the incomplete body. That must remain a swallowed,
    boolean-returning non-event on the boot path — the whole design is that a
    workspace which cannot report its plugin state still boots with the plugins it
    has. The 400 is warned about (it means a runtime bug) but never raised.
    """
    _install(monkeypatch, _Recorder(response=httpx.Response(400)))
    with caplog.at_level(logging.WARNING, logger=pir.__name__):
        assert pir.report_install_outcome(_report_missing("swapped")) is False  # type: ignore[arg-type]
    assert "400" in caplog.text


def test_a_drifted_report_is_still_sent_so_core_can_refuse_it(monkeypatch):
    """Deliberately NOT "refuse to send locally".

    Dropping the POST would leave core with no row at all — indistinguishable from
    a box that never booted the reporting runtime. Sending the incomplete body
    makes core log a 400 for THIS workspace id, which is the signal an operator can
    actually reach.
    """
    rec = _install(monkeypatch, _Recorder(response=httpx.Response(400)))
    pir.report_install_outcome(_report_missing("declared"))  # type: ignore[arg-type]
    assert len(rec.calls) == 1, "the drifted report must still reach core to be refused"
    assert "declared" not in rec.calls[0]["json"]
    assert rec.calls[0]["json"]["swapped"] is True, "the fields we DO have still ship"


# --- the vendored contract is a mirror, not a fork -------------------------


def test_vendored_contract_pins_the_endpoint():
    assert pir.path_template() == "/workspaces/{workspace_id}/plugin-install-report"
    assert pir.http_method() == "POST"
    assert pir.success_status() == 204


def test_vendored_contract_names_exactly_the_install_report_fields():
    assert set(pir.field_names()) == {
        "declared",
        "plugins_dir",
        "installed",
        "skipped",
        "failed",
        "swapped",
        # core#5007 — OPTIONAL on the wire (absent from the schema's `required`)
        # but NAMED by the contract, so the producer must project it. Absent
        # means "this box cannot tell me", never "nothing installed".
        "installed_refs",
    }


def test_install_report_dataclass_still_matches_the_contract():
    """The one cross-check that catches the real drift: a field added to
    InstallReport without landing in the contract would be silently unreported."""
    from molecule_runtime.plugin_sources import InstallReport

    attrs = set(InstallReport.__dataclass_fields__)
    assert attrs == set(pir.field_names()), (
        f"InstallReport fields {sorted(attrs)} != contract fields "
        f"{sorted(pir.field_names())} — one side changed alone"
    )
