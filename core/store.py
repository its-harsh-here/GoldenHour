"""SQLite persistence shared by the runtime (cases, trace events, audit chain, outbox, LLM cache)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, pack TEXT, status TEXT, title TEXT, hero TEXT,
  received_at REAL, updated_at REAL, decided_at REAL,
  sla_paused_at REAL, sla_paused_seconds REAL DEFAULT 0,
  assigned_role TEXT, recommendation TEXT, recommended_amount INTEGER, final_amount INTEGER,
  risk_score INTEGER, override INTEGER DEFAULT 0, seeded INTEGER DEFAULT 0,
  request_json TEXT, state_json TEXT
);
CREATE TABLE IF NOT EXISTS trace_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, ts REAL, agent TEXT, step TEXT,
  tool TEXT, input_summary TEXT, output_json TEXT, latency_ms INTEGER, tokens INTEGER,
  model TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_case ON trace_events(case_id, id);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, case_id TEXT, actor TEXT, role TEXT,
  action TEXT, payload TEXT, prev_hash TEXT, hash TEXT
);
CREATE TABLE IF NOT EXISTS audit_tamper_backup (id INTEGER PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, kind TEXT, channel TEXT, recipient TEXT,
  subject TEXT, body_json TEXT, created_at REAL, actor TEXT
);
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, response_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

RESETTABLE = ("cases", "trace_events", "audit_log", "audit_tamper_backup", "outbox", "kv")


class Store:
    """One shared connection guarded by a re-entrant lock: simple and safe for demo-scale load."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid or cur.rowcount

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def reset(self) -> None:
        with self._lock:
            for table in RESETTABLE:
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('trace_events','audit_log','outbox')")
            self._conn.commit()

    # -- key/value settings (autonomy level, kill switch) --
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))

    # -- LLM response cache keyed by sha256(model + messages) --
    def cache_get(self, key: str) -> dict | None:
        row = self.one("SELECT response_json FROM llm_cache WHERE key=?", (key,))
        return json.loads(row["response_json"]) if row else None

    def cache_put(self, key: str, model: str, response: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO llm_cache(key, model, response_json, created_at) VALUES(?,?,?,?)",
            (key, model, json.dumps(response), time.time()),
        )
