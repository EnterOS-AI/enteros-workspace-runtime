"""Contract for the plugin boot-install report (producer side).

The property under test is NOT "does an HTTP POST happen". It is "can the platform
find out that a workspace booted with no live plugins" — because before this module
it could not, for any ordinary workspace, and molecule-core#4953 spent three
proposed-and-retracted explanations on a symptom whose cause was unobservable.

So the tests concentrate on the four ways this could look like it works and not:

 1. **gating** — if this ever becomes concierge-gated it recreates the exact blind
    spot it was written to close, and the change would look like noise reduction
 2. **fail-soft** — if a report failure can raise, observability has become a boot
    dependency and a network blip stops a workspace booting
 3. **the payload** — if the wire shape is hand-written rather than driven by the
    contract, a rename silently sends a field the receiver ignores
 4. **`swapped`** — the field that distinguishes "staged" from "live"; dropping it
    turns a total failure into an apparent success

``httpx.request`` is monkeypatched rather than the client class: the module calls
the module-level function, which is the honest seam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from molecule_runtime import plugin_install_report as pir


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
    """Even a garbage input must not raise into boot."""
    _install(monkeypatch, _Recorder())
    assert pir.report_install_outcome(object()) in (True, False)  # type: ignore[arg-type]


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
