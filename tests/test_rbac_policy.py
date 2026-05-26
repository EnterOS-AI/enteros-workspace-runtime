"""Tests for molecule_runtime.rbac_policy."""

from __future__ import annotations

from molecule_runtime import rbac_policy


class TestRolePermissions:
    def test_admin_has_all_actions(self) -> None:
        """admin role grants all built-in actions."""
        for action in ("delegate", "approve", "memory.read", "memory.write", "display.control"):
            assert rbac_policy.check_permission(action, ["admin"]) is True

    def test_operator_has_delegate_approve_read_write(self) -> None:
        """operator role grants delegate, approve, memory."""
        for action in ("delegate", "approve", "memory.read", "memory.write", "display.control"):
            assert rbac_policy.check_permission(action, ["operator"]) is True

    def test_readonly_has_memory_read_only(self) -> None:
        """read-only role grants memory.read, nothing else."""
        assert rbac_policy.check_permission("memory.read", ["read-only"]) is True
        assert rbac_policy.check_permission("delegate", ["read-only"]) is False
        assert rbac_policy.check_permission("approve", ["read-only"]) is False
        assert rbac_policy.check_permission("memory.write", ["read-only"]) is False

    def test_no_delegation_blocks_delegate(self) -> None:
        """no-delegation role blocks delegate, grants approve + memory + display."""
        assert rbac_policy.check_permission("delegate", ["no-delegation"]) is False
        assert rbac_policy.check_permission("approve", ["no-delegation"]) is True
        assert rbac_policy.check_permission("memory.read", ["no-delegation"]) is True
        assert rbac_policy.check_permission("display.control", ["no-delegation"]) is True

    def test_no_approval_blocks_approve(self) -> None:
        """no-approval role blocks approve, grants everything else."""
        assert rbac_policy.check_permission("approve", ["no-approval"]) is False
        assert rbac_policy.check_permission("delegate", ["no-approval"]) is True

    def test_memory_readonly_matches_readonly(self) -> None:
        """memory-readonly is alias for read-only."""
        assert rbac_policy.check_permission("memory.read", ["memory-readonly"]) is True
        assert rbac_policy.check_permission("delegate", ["memory-readonly"]) is False

    def test_unknown_role_grants_nothing(self) -> None:
        """Unknown role has no permissions."""
        assert rbac_policy.check_permission("memory.read", ["unknown-role"]) is False

    def test_first_matching_role_wins(self) -> None:
        """Roles are checked in order; first match returns True."""
        # admin always returns True (short-circuit)
        assert rbac_policy.check_permission("delegate", ["admin", "read-only"]) is True

    def test_custom_permissions_override_builtin(self) -> None:
        """custom_permissions replaces built-in for non-admin roles.

        Note: admin short-circuits before custom_permissions check —
        admin always returns True (built-in behavior, not overridable).
        """
        custom = {"operator": {"memory.read"}}  # strip operator's delegate
        assert rbac_policy.check_permission("memory.read", ["operator"], custom) is True
        assert rbac_policy.check_permission("delegate", ["operator"], custom) is False

    def test_custom_permission_not_in_builtin(self) -> None:
        """Custom role can grant actions not in built-in set."""
        custom = {"custom-role": ["memory.write", "approve"]}
        assert rbac_policy.check_permission("memory.write", ["custom-role"], custom) is True
        assert rbac_policy.check_permission("delegate", ["custom-role"], custom) is False

    def test_empty_roles_returns_false(self) -> None:
        """Empty roles list grants nothing."""
        assert rbac_policy.check_permission("memory.read", []) is False

    def test_admin_grants_unknown_action(self) -> None:
        """admin short-circuits to True for ANY action (not just built-in set)."""
        assert rbac_policy.check_permission("nonexistent-action", ["admin"]) is True