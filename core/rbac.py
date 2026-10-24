"""Role-based access control. Roles and permissions come from the pack's pack.yaml."""
from __future__ import annotations


class Forbidden(Exception):
    def __init__(self, role: str, permission: str, reason: str = ""):
        self.role, self.permission = role, permission
        self.reason = reason or f"role {role} is not allowed to {permission.replace('_', ' ')}"
        super().__init__(self.reason)


class RBAC:
    def __init__(self, roles: dict, permissions: dict):
        self.roles = roles
        self.permissions = {action: set(allowed) for action, allowed in permissions.items()}

    def can(self, role: str, permission: str) -> bool:
        return role in self.roles and role in self.permissions.get(permission, set())

    def require(self, role: str, permission: str) -> None:
        if not self.can(role, permission):
            raise Forbidden(role, permission)

    def has_phi_access(self, role: str) -> bool:
        return bool(self.roles.get(role, {}).get("phi_access"))

    def public_roles(self) -> list[dict]:
        return [{"id": rid, "label": r["label"], "phi_access": bool(r.get("phi_access"))}
                for rid, r in self.roles.items() if not r.get("internal")]
