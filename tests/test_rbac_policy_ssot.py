"""Regression coverage for the runtime RBAC policy SSOT."""


def test_audit_and_a2a_rbac_share_role_permissions_object():
    import molecule_runtime.a2a_tools_rbac as a2a_rbac
    import molecule_runtime.builtin_tools.audit as audit
    import molecule_runtime.rbac_policy as policy

    assert audit.ROLE_PERMISSIONS is policy.ROLE_PERMISSIONS
    assert a2a_rbac.ROLE_PERMISSIONS is policy.ROLE_PERMISSIONS


def test_check_permission_uses_custom_role_as_authoritative():
    from molecule_runtime.rbac_policy import check_permission

    custom = {"operator": ["memory.read"]}
    assert check_permission("memory.read", ["operator"], custom)
    assert not check_permission("delegate", ["operator"], custom)
