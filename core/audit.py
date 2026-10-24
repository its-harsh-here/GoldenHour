"""Tamper-evident audit log: each block stores hash = sha256(prev_hash + payload).

`verify()` walks the chain and reports the first block whose stored hash no longer matches.
`tamper()` (Admin demo) edits one stored payload without fixing its hash; `restore()` undoes it.
"""
from __future__ import annotations

import hashlib
import json
import time

from core.store import Store

GENESIS = "0" * 64


def _hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, store: Store):
        self.store = store

    def append(self, *, case_id: str | None, actor: str, role: str, action: str, details: dict | None = None) -> dict:
        with self.store.lock:
            last = self.store.one("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS
            ts = round(time.time(), 3)
            payload = json.dumps({"ts": ts, "case_id": case_id, "actor": actor, "role": role, "action": action,
                                  "details": details or {}}, sort_keys=True, ensure_ascii=False, default=str)
            digest = _hash(prev, payload)
            block_id = self.store.execute(
                "INSERT INTO audit_log(ts, case_id, actor, role, action, payload, prev_hash, hash) VALUES(?,?,?,?,?,?,?,?)",
                (ts, case_id, actor, role, action, payload, prev, digest),
            )
        return {"id": block_id, "hash": digest, "prev_hash": prev}

    def blocks(self, case_id: str | None = None, limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM audit_log" + (" WHERE case_id=?" if case_id else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.store.query(sql, (case_id, limit) if case_id else (limit,))
        for row in rows:
            row["details"] = json.loads(row["payload"]).get("details", {})
        return list(reversed(rows))

    def verify(self) -> dict:
        prev, checked, broken_at, reason = GENESIS, 0, None, None
        results = []
        for row in self.store.query("SELECT id, payload, prev_hash, hash FROM audit_log ORDER BY id"):
            checked += 1
            ok = row["prev_hash"] == prev and row["hash"] == _hash(prev, row["payload"])
            if not ok and broken_at is None:
                broken_at = row["id"]
                reason = ("payload no longer matches its stored hash" if row["prev_hash"] == prev
                          else "link to the previous block is broken")
            results.append({"id": row["id"], "ok": ok and broken_at is None})
            prev = row["hash"]
        return {"valid": broken_at is None, "checked": checked, "broken_at": broken_at, "reason": reason, "blocks": results}

    def tamper(self, block_id: int | None = None) -> dict:
        """Demo only: silently alter one stored payload (as an insider with DB access might)."""
        rows = self.store.query("SELECT id, payload FROM audit_log ORDER BY id")
        if not rows:
            raise ValueError("audit log is empty")
        decisions = [r for r in rows if '"action": "approve"' in r["payload"] or '"action": "auto_approve"' in r["payload"]]
        target = next((r for r in rows if r["id"] == block_id), None) if block_id else None
        target = target or (decisions[-1] if decisions else rows[len(rows) // 2])
        self.store.execute("INSERT OR IGNORE INTO audit_tamper_backup(id, payload) VALUES(?, ?)", (target["id"], target["payload"]))
        data = json.loads(target["payload"])
        details = data.setdefault("details", {})
        if isinstance(details.get("amount"), (int, float)):
            details["amount"] = int(details["amount"]) * 3
        else:
            details["note"] = "edited after the fact"
        altered = json.dumps(data, sort_keys=True, ensure_ascii=False)
        self.store.execute("UPDATE audit_log SET payload=? WHERE id=?", (altered, target["id"]))
        return {"tampered_block": target["id"], "change": details}

    def restore(self) -> dict:
        backups = self.store.query("SELECT id, payload FROM audit_tamper_backup")
        for b in backups:
            self.store.execute("UPDATE audit_log SET payload=? WHERE id=?", (b["payload"], b["id"]))
        self.store.execute("DELETE FROM audit_tamper_backup")
        return {"restored_blocks": [b["id"] for b in backups]}
