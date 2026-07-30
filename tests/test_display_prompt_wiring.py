"""The desktop display-control section must be wired into the production system
prompt (regression: get_display_instructions existed but was never injected, so
agents reported themselves as "a server-side agent without a display" even though
the sidecar/gateway were available). It is gated on the display.control RBAC
action so action-capable agents get it and genuinely read-only agents do not.
"""

import molecule_runtime.builtin_tools.audit as audit
from molecule_runtime.prompt import build_system_prompt

_SECTION = "## Desktop Display Control"


def test_display_section_injected_for_action_capable_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "get_workspace_roles", lambda: (["operator"], {}))
    monkeypatch.setattr(
        audit,
        "check_permission",
        lambda action, roles, custom=None: action == "display.control",
    )
    out = build_system_prompt(
        config_path=str(tmp_path), workspace_id="w", loaded_skills=[], peers=[]
    )
    assert _SECTION in out, "display-control section must be injected when display.control is granted"
    # And it names the actual tools the agent can call.
    assert "desktop_screenshot" in out


def test_display_section_omitted_for_readonly_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "get_workspace_roles", lambda: (["read-only"], {}))
    monkeypatch.setattr(
        audit, "check_permission", lambda action, roles, custom=None: False
    )
    out = build_system_prompt(
        config_path=str(tmp_path), workspace_id="w", loaded_skills=[], peers=[]
    )
    assert _SECTION not in out, "read-only agents lack display.control and must not be told they have a screen"


def test_prompt_assembly_survives_rbac_lookup_failure(tmp_path, monkeypatch):
    # The optional section must never break prompt assembly.
    def _boom():
        raise RuntimeError("rbac config unavailable")

    monkeypatch.setattr(audit, "get_workspace_roles", _boom)
    out = build_system_prompt(
        config_path=str(tmp_path), workspace_id="w", loaded_skills=[], peers=[]
    )
    assert _SECTION not in out  # skipped, not fatal
    assert out  # prompt still built
