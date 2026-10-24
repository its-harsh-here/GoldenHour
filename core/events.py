"""Trace events: every agent step is written to SQLite and streamed to the UI over SSE."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable

from core.store import Store


def _summary(value: Any, limit: int = 1500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class EventLog:
    def __init__(self, store: Store):
        self.store = store

    def emit(self, case_id: str, agent: str, step: str, *, tool: str | None = None, input_summary: str = "",
             output: Any = None, latency_ms: int = 0, tokens: int = 0, model: str = "", status: str = "ok") -> int:
        return self.store.execute(
            "INSERT INTO trace_events(case_id, ts, agent, step, tool, input_summary, output_json, latency_ms, tokens, model, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, time.time(), agent, step, tool, input_summary[:300], _summary(output), latency_ms, tokens, model, status),
        )

    def list(self, case_id: str, after_id: int = 0) -> list[dict]:
        rows = self.store.query("SELECT * FROM trace_events WHERE case_id=? AND id>? ORDER BY id", (case_id, after_id))
        for row in rows:
            try:
                row["output"] = json.loads(row.pop("output_json") or "null")
            except json.JSONDecodeError:
                row["output"] = None
        return rows

    # ---- desk-wide notifications (arrivals, settled cases, decisions, admin changes) ----
    def notify(self, case_id: str | None, kind: str, summary: dict | None = None) -> int:
        return self.store.execute("INSERT INTO notifications(ts, case_id, kind, summary_json) VALUES(?,?,?,?)",
                                  (time.time(), case_id, kind, json.dumps(summary or {}, ensure_ascii=False, default=str)))

    def last_notification_id(self) -> int:
        row = self.store.one("SELECT MAX(id) AS n FROM notifications")
        return int(row["n"] or 0)

    def notifications(self, after: int = 0, limit: int = 200) -> list[dict]:
        rows = self.store.query("SELECT * FROM notifications WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        for row in rows:
            row["summary"] = json.loads(row.pop("summary_json") or "{}")
        return rows

    async def desk_stream(self, after: int | None = None, max_seconds: float = 900) -> AsyncIterator[str]:
        """Server-sent `feed` events for the whole desk; clients reconnect with Last-Event-ID."""
        last = self.last_notification_id() if after is None else after
        started = ping = time.monotonic()
        yield "retry: 2000\n\n"
        while time.monotonic() - started < max_seconds:
            rows = self.notifications(last)
            if not rows and last > self.last_notification_id():  # the desk was reset under us
                last = 0
                continue
            for row in rows:
                last = row["id"]
                yield f"id: {row['id']}\nevent: feed\ndata: {json.dumps(row, ensure_ascii=False, default=str)}\n\n"
            if time.monotonic() - ping > 15:
                ping = time.monotonic()
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    async def stream(self, case_id: str, status_of: Callable[[str], str | None], after_id: int = 0,
                     busy: tuple[str, ...] = ("PROCESSING",), max_seconds: float = 600) -> AsyncIterator[str]:
        """Server-sent events: `trace` per event, `status` on change, `done` once the case settles."""
        last, last_status, idle, started, ping = after_id, None, 0.0, time.monotonic(), time.monotonic()
        yield "retry: 1500\n\n"
        while time.monotonic() - started < max_seconds:
            rows = self.list(case_id, last)
            for row in rows:
                last = row["id"]
                yield f"id: {row['id']}\nevent: trace\ndata: {json.dumps(row, ensure_ascii=False, default=str)}\n\n"
            status = status_of(case_id)
            if status != last_status:
                last_status = status
                yield f"event: status\ndata: {json.dumps({'status': status})}\n\n"
            idle = 0.0 if rows or status in busy else idle + 0.3
            if idle >= 1.5:
                yield f"event: done\ndata: {json.dumps({'status': status, 'last_id': last})}\n\n"
                return
            if time.monotonic() - ping > 10:
                ping = time.monotonic()
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.3)
