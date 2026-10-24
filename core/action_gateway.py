"""Action Gateway: the only path from a decision to a side effect.

Every action is checked against RBAC; rejected attempts raise Forbidden (HTTP 403) and are written
to the audit chain. Allowed actions run the pack's handler and are audited with their result.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from core.audit import AuditLog
from core.events import EventLog
from core.rbac import RBAC, Forbidden
from core.store import Store

Handler = Callable[[str, dict, str, str], dict]


class ActionGateway:
    def __init__(self, rbac: RBAC, audit: AuditLog, store: Store, events: EventLog):
        self.rbac, self.audit, self.store, self.events = rbac, audit, store, events
        self._handlers: dict[str, Handler] = {}

    def register(self, action: str, handler: Handler) -> None:
        self._handlers[action] = handler

    def execute(self, action: str, *, case_id: str | None, role: str, actor: str,
                params: dict | None = None, permission: str | None = None) -> dict:
        permission = permission or action
        if not self.rbac.can(role, permission):
            err = Forbidden(role, permission)
            self.audit.append(case_id=case_id, actor=actor, role=role, action="action_blocked",
                              details={"attempted": action, "permission": permission, "reason": err.reason})
            if case_id:
                self.events.emit(case_id, "action_gateway", "blocked", tool=action, status="blocked",
                                 input_summary=f"{role} attempted {action}", output={"http": 403, "reason": err.reason})
            raise err
        handler = self._handlers.get(action)
        if handler is None:
            raise KeyError(f"no handler registered for action '{action}'")
        result = handler(case_id or "", params or {}, role, actor)
        self.audit.append(case_id=case_id, actor=actor, role=role, action=action, details=result.get("audit", result))
        return result

    # ---- outbox: every outbound message the gateway sends ----
    def outbox_add(self, case_id: str, kind: str, channel: str, recipient: str, subject: str, body: dict, actor: str) -> int:
        return self.store.execute(
            "INSERT INTO outbox(case_id, kind, channel, recipient, subject, body_json, created_at, actor) VALUES(?,?,?,?,?,?,?,?)",
            (case_id, kind, channel, recipient, subject, json.dumps(body, ensure_ascii=False), time.time(), actor),
        )

    def outbox(self, case_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM outbox" + (" WHERE case_id=?" if case_id else "") + " ORDER BY id"
        rows = self.store.query(sql, (case_id,) if case_id else ())
        for row in rows:
            row["body"] = json.loads(row.pop("body_json"))
        return rows
