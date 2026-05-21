"""Single source of truth for runtime RBAC role permissions."""

from __future__ import annotations


# Built-in role -> permitted action mappings.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"delegate", "approve", "memory.read", "memory.write"},
    "operator": {"delegate", "approve", "memory.read", "memory.write"},
    "read-only": {"memory.read"},
    "no-delegation": {"approve", "memory.read", "memory.write"},
    "no-approval": {"delegate", "memory.read", "memory.write"},
    "memory-readonly": {"memory.read"},
}


def check_permission(
    action: str,
    roles: list[str],
    custom_permissions: dict[str, list[str]] | None = None,
) -> bool:
    """Return True if any role grants ``action``.

    Custom role definitions are authoritative for that role: when a role
    appears in ``custom_permissions``, its built-in entry is ignored.
    """
    for role in roles:
        if role == "admin":
            return True
        if custom_permissions and role in custom_permissions:
            if action in custom_permissions[role]:
                return True
            continue
        if action in ROLE_PERMISSIONS.get(role, set()):
            return True
    return False
