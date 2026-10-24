"""Domain-agnostic governed agent runtime: a plain-Python state machine over a pack's pipeline.

A pack supplies an ordered list of Steps (agents). The orchestrator runs them, persists the case
state after every step (so a case can pause and resume), emits trace + audit events, and turns any
step failure into a human-routed case instead of failing silently.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from core.action_gateway import ActionGateway
from core.audit import AuditLog
from core.events import EventLog
from core.llm import LLMClient
from core.rbac import RBAC
from core.settings import Settings
from core.store import Store


@dataclass
class StepContext:
    """Everything one agent step may use. Agents return a dict merged into the case state."""

    case_id: str
    request: dict
    state: dict
    llm: LLMClient
    settings: Settings
    config: dict
    emit: Callable[..., None]
    force_mock: bool = False
    runtime: Any = None

    def model_label(self, model: str, source: str) -> str:
        return model if source in ("groq", "cache", "replay") else f"{source}:{model.split('/')[-1]}"


@dataclass
class Step:
    name: str
    fn: Callable[[StepContext], dict]
    agent: str
    always: bool = False  # still runs after an earlier step failed (decision + routing)


class CaseStore:
    FIELDS = ("pack", "status", "title", "hero", "received_at", "updated_at", "decided_at", "sla_paused_at",
              "sla_paused_seconds", "assigned_role", "recommendation", "recommended_amount", "final_amount",
              "risk_score", "override", "seeded", "request_json", "state_json")

    def __init__(self, store: Store):
        self.store = store

    def create(self, case_id: str, pack: str, request: dict, title: str, hero: str | None, received_at: float) -> None:
        self.store.execute(
            "INSERT OR REPLACE INTO cases(case_id, pack, status, title, hero, received_at, updated_at, request_json, state_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (case_id, pack, "NEW", title, hero, received_at, time.time(), json.dumps(request, ensure_ascii=False), "{}"),
        )

    @staticmethod
    def _parse(row: dict) -> dict:
        row["request"] = json.loads(row.pop("request_json") or "{}")
        row["state"] = json.loads(row.pop("state_json") or "{}")
        return row

    def get(self, case_id: str) -> dict | None:
        row = self.store.one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        return self._parse(row) if row else None

    def list(self) -> list[dict]:
        return [self._parse(r) for r in self.store.query("SELECT * FROM cases ORDER BY received_at DESC, case_id")]

    def status(self, case_id: str) -> str | None:
        row = self.store.one("SELECT status FROM cases WHERE case_id=?", (case_id,))
        return row["status"] if row else None

    def update(self, case_id: str, **fields: Any) -> None:
        bad = set(fields) - set(self.FIELDS)
        if bad:
            raise ValueError(f"unknown case fields: {bad}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.store.execute(f"UPDATE cases SET {cols} WHERE case_id=?", (*fields.values(), case_id))

    def save_state(self, case_id: str, state: dict) -> None:
        self.update(case_id, state_json=json.dumps(state, ensure_ascii=False, default=str))

    def save_request(self, case_id: str, request: dict) -> None:
        self.update(case_id, request_json=json.dumps(request, ensure_ascii=False))


def sla_info(case: dict, sla_minutes: int, now: float | None = None) -> dict:
    """Minutes left on the decision clock; paused while waiting on the hospital."""
    now = now or time.time()
    end = case.get("decided_at") or now
    paused = case.get("sla_paused_seconds") or 0
    if case.get("sla_paused_at") and not case.get("decided_at"):
        paused += now - case["sla_paused_at"]
    elapsed = max(0.0, end - (case.get("received_at") or now) - paused)
    budget = sla_minutes * 60
    return {"elapsed_s": round(elapsed, 1), "remaining_s": round(budget - elapsed, 1), "budget_s": budget,
            "fraction": round(min(elapsed / budget, 1.0), 4), "breached": elapsed > budget,
            "paused": bool(case.get("sla_paused_at")) and not case.get("decided_at"), "done": bool(case.get("decided_at"))}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Orchestrator:
    def __init__(self, runtime: "Runtime"):
        self.rt = runtime
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def is_running(self, case_id: str) -> bool:
        return case_id in self._running

    def run(self, case_id: str, from_step: str | None = None, force_mock: bool = False) -> dict:
        with self._lock:
            if case_id in self._running:
                return {"status": "already_running"}
            self._running.add(case_id)
        try:
            return self._run(case_id, from_step, force_mock)
        finally:
            with self._lock:
                self._running.discard(case_id)

    def _run(self, case_id: str, from_step: str | None, force_mock: bool) -> dict:
        rt, pack = self.rt, self.rt.pack
        case = rt.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        state, request = case["state"], case["request"]
        state.pop("step_failure", None)
        names = [s.name for s in pack.pipeline]
        start = names.index(from_step) if from_step else 0
        rt.cases.update(case_id, status="PROCESSING", assigned_role=None)
        rt.events.emit(case_id, "orchestrator", "resume" if from_step else "start", input_summary=f"pipeline from '{names[start]}'",
                       output={"steps": names[start:], "llm_mode": "mock (seed)" if force_mock else rt.llm.mode})
        pause = None
        for step in pack.pipeline[start:]:
            if state.get("step_failure") and not step.always:
                continue
            ctx = StepContext(case_id, request, state, rt.llm, rt.settings, pack.config,
                              lambda agent, name, **kw: rt.events.emit(case_id, agent, name, **kw), force_mock, rt)
            t0 = time.perf_counter()
            try:
                out = step.fn(ctx) or {}
            except Exception as err:  # graceful degradation: any failure goes to a human with the reason
                state["step_failure"] = {"step": step.name, "agent": step.agent, "reason": f"{type(err).__name__}: {str(err)[:240]}"}
                rt.events.emit(case_id, step.agent, "step_failed", status="error", output=state["step_failure"],
                               latency_ms=int((time.perf_counter() - t0) * 1000))
                rt.audit.append(case_id=case_id, actor=f"agent:{step.agent}", role="SYSTEM", action="step_failed", details=state["step_failure"])
                rt.cases.save_state(case_id, state)
                continue
            pause = out.pop("_pause", None) or pause
            resume_at = out.pop("_resume_at", None)
            for key, value in rt.cases.get(case_id)["state"].items():  # keep what gateway handlers saved meanwhile
                state.setdefault(key, value)
            state.update(out)
            if resume_at:
                state["resume_at"] = resume_at
            rt.cases.save_state(case_id, state)
            rt.audit.append(case_id=case_id, actor=f"agent:{step.agent}", role="SYSTEM", action=f"step:{step.name}",
                            details={"output_digest": _digest(out), **pack.audit_summary(step.name, out)})
        status = rt.cases.status(case_id)
        rt.events.emit(case_id, "orchestrator", "paused" if pause else "settled", output={"status": status},
                       status="paused" if pause else "ok")
        return {"status": status, "paused": bool(pause)}

    def run_in_background(self, case_id: str, from_step: str | None = None) -> bool:
        if self.is_running(case_id):
            return False
        threading.Thread(target=self.run, args=(case_id, from_step), daemon=True, name=f"case-{case_id}").start()
        return True


def load_pack(name: str):
    return importlib.import_module(f"packs.{name}").PACK


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


class Runtime:
    """Wires the governed runtime together for one pack."""

    def __init__(self, settings: Settings, pack=None):
        self.settings = settings
        self.pack = pack or load_pack(settings.pack)
        self.store = Store(settings.db_path)
        self.llm = LLMClient(settings, self.store, self.pack.replay_path)
        self.events = EventLog(self.store)
        self.audit = AuditLog(self.store)
        self.rbac = RBAC(self.pack.config["roles"], self.pack.config["permissions"])
        self.gateway = ActionGateway(self.rbac, self.audit, self.store, self.events)
        self.cases = CaseStore(self.store)
        self.orchestrator = Orchestrator(self)
        self.pack.register_actions(self)
        self.gateway.register("set_autonomy", self._set_autonomy)
        self.gateway.register("kill_switch", self._kill_switch)
        self.gateway.register("tamper_test", lambda _cid, params, _role, _actor: self.audit.tamper(params.get("block_id")))
        self.gateway.register("audit_restore", lambda _cid, _params, _role, _actor: self.audit.restore())

    def _set_autonomy(self, _cid: str, params: dict, _role: str, _actor: str) -> dict:
        level = int(params.get("level", 0))
        if level not in (1, 2, 3):
            raise ValueError("autonomy level must be 1, 2 or 3")
        self.store.kv_set("autonomy_level", level)
        return {"autonomy_level": self.autonomy_level, "requested": level, "kill_switch": self.kill_switch}

    def _kill_switch(self, _cid: str, params: dict, _role: str, _actor: str) -> dict:
        self.store.kv_set("kill_switch", bool(params.get("engaged", True)))
        return {"kill_switch": self.kill_switch, "autonomy_level": self.autonomy_level}

    @property
    def sla_minutes(self) -> int:
        return int(self.pack.config["sla"]["decision_minutes"])

    @property
    def kill_switch(self) -> bool:
        return bool(self.store.kv_get("kill_switch", False))

    @property
    def autonomy_level(self) -> int:
        if self.kill_switch:
            return 1
        return int(self.store.kv_get("autonomy_level", self.pack.config["decision_policy"]["autonomy_level"]))
