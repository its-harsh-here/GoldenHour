"""GoldenHour HTTP API and static UI.

The API is pack-agnostic: it talks to core/ (runtime, gateway, audit, events) and to the pack's
hooks (seed, decide, case views, dashboard). The demo role switcher sends the role in `X-Role`.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.orchestrator import Runtime
from core.rbac import Forbidden
from core.settings import Settings, get_settings

STATIC = Path(__file__).resolve().parent / "static"
BUSY = ("PROCESSING",)


class DecisionBody(BaseModel):
    action: str
    amount: int | None = None
    reason: str = ""


class AutonomyBody(BaseModel):
    level: int


class KillSwitchBody(BaseModel):
    engaged: bool = True


class ResetBody(BaseModel):
    history: bool = True


class TamperBody(BaseModel):
    block_id: int | None = None


class FeedBody(BaseModel):
    enabled: bool = True


class DayBody(BaseModel):
    minutes: float | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        rt = Runtime(s)
        if not rt.cases.list():
            rt.pack.seed(rt, history=s.seed_history)
        if not rt.cases.list():
            print(f"ERROR: pack '{rt.pack.name}' loaded zero cases; its data directory is missing from this build.", flush=True)
        app.state.rt = rt
        if s.live_feed:
            rt.feed.start()
            if s.day_backfill:
                rt.feed.start_day()  # the last 24 hours, processed as fast as the machine allows
        yield
        rt.feed.stop()

    app = FastAPI(title="GoldenHour", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def rt_of(request: Request) -> Runtime:
        return request.app.state.rt

    def role_of(x_role: str | None) -> str:
        return (x_role or "PROCESSOR").strip().upper()

    def actor_of(role: str, x_actor: str | None) -> str:
        return x_actor or f"demo.{role.lower()}"

    def case_or_404(rt: Runtime, case_id: str) -> dict:
        case = rt.cases.get(case_id)
        if case is None:
            raise HTTPException(404, f"unknown case {case_id}")
        return case

    @app.exception_handler(Forbidden)
    async def forbidden(_request: Request, err: Forbidden):
        return JSONResponse(status_code=403, content={"detail": err.reason, "role": err.role, "permission": err.permission})

    @app.middleware("http")
    async def decision_errors(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as err:  # pack DecisionError carries its own status code
            code = getattr(err, "status_code", None)
            if isinstance(code, int):
                return JSONResponse(status_code=code, content={"detail": str(err)})
            raise

    # ---------------- pages ----------------
    @app.get("/", include_in_schema=False)
    def landing() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(STATIC / "console.html")

    # ---------------- meta ----------------
    @app.get("/api/health")
    def health(request: Request) -> dict:
        rt = rt_of(request)
        cases = rt.store.one("SELECT COUNT(*) AS n FROM cases")["n"]
        return {"ok": cases > 0, "llm_mode": rt.llm.mode, "cases": cases, "replay_entries": len(rt.llm.replay),
                "feed": {"enabled": rt.feed.enabled, "running": rt.feed.running}}

    @app.get("/api/meta")
    def meta(request: Request) -> dict:
        rt = rt_of(request)
        cfg = rt.pack.config
        return {
            "llm_mode": rt.llm.mode, "llm_mode_note": rt.settings.llm_mode_note,
            "model_roles": ["fast", "reason", "guard"],
            "replay_entries": len(rt.llm.replay),
            "autonomy_level": rt.autonomy_level, "kill_switch": rt.kill_switch,
            "roles": rt.rbac.public_roles(),
            "permissions": {action: sorted(roles) for action, roles in rt.rbac.permissions.items()},
            "pack": {"name": rt.pack.name, "title": cfg["title"], "domain": cfg["domain"], "version": cfg["version"]},
            "sla_minutes": rt.sla_minutes, "threshold_inr": cfg["decision_policy"]["auto_threshold_inr"],
            "hero_cases": [r["case_id"] for r in rt.store.query("SELECT case_id FROM cases WHERE hero IS NOT NULL ORDER BY hero")],
            "feed": rt.feed.status(), "day_minutes": rt.settings.day_replay_minutes,
        }

    # ---------------- cases ----------------
    @app.get("/api/cases")
    def list_cases(request: Request, x_role: str | None = Header(None)) -> list[dict]:
        rt, role = rt_of(request), role_of(x_role)
        return [rt.pack.case_summary(rt, c, role) for c in rt.cases.list_light()]

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, request: Request, x_role: str | None = Header(None)) -> dict:
        rt = rt_of(request)
        return rt.pack.case_view(rt, case_or_404(rt, case_id), role_of(x_role))

    @app.post("/api/cases/{case_id}/process")
    def process(case_id: str, request: Request, sync: bool = False, x_role: str | None = Header(None),
                x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        case = case_or_404(rt, case_id)
        rt.rbac.require(role, "process_case")
        if case["status"] != "NEW":
            raise HTTPException(409, f"case is {case['status']}; it has already been processed")
        rt.audit.append(case_id=case_id, actor=actor_of(role, x_actor), role=role, action="process_requested", details={"llm_mode": rt.llm.mode})
        if sync:
            return rt.orchestrator.run(case_id)
        return {"started": rt.orchestrator.run_in_background(case_id)}

    @app.get("/api/cases/{case_id}/events")
    def events(case_id: str, request: Request, after: int = 0) -> list[dict]:
        rt = rt_of(request)
        case_or_404(rt, case_id)
        return rt.events.list(case_id, after)

    @app.get("/api/cases/{case_id}/stream")
    async def stream(case_id: str, request: Request, after: int = 0) -> StreamingResponse:
        rt = rt_of(request)
        case_or_404(rt, case_id)

        def status_of(cid: str) -> str | None:
            return "PROCESSING" if rt.orchestrator.is_running(cid) else rt.cases.status(cid)

        return StreamingResponse(rt.events.stream(case_id, status_of, after, BUSY), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/cases/{case_id}/decision")
    def decision(case_id: str, body: DecisionBody, request: Request, x_role: str | None = Header(None),
                 x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        case_or_404(rt, case_id)
        result = rt.pack.decide(rt, case_id, role, actor_of(role, x_actor), body.action, body.amount, body.reason)
        return {"ok": True, **{k: v for k, v in result.items() if k != "audit"}}

    @app.post("/api/cases/{case_id}/simulate-upload")
    def simulate_upload(case_id: str, request: Request, sync: bool = False, x_role: str | None = Header(None),
                        x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        case_or_404(rt, case_id)
        out = rt.gateway.execute("simulate_upload", case_id=case_id, role=role, actor=actor_of(role, x_actor))
        if sync:
            return {**rt.orchestrator.run(case_id, from_step=out["resume_at"]), "received": out["received"]}
        rt.orchestrator.run_in_background(case_id, from_step=out["resume_at"])
        return {"started": True, "received": out["received"]}

    @app.get("/api/outbox")
    def outbox(request: Request, case_id: str | None = None, x_role: str | None = Header(None)) -> list[dict]:
        rt = rt_of(request)
        rows = rt.gateway.outbox(case_id)
        return rows if rt.rbac.has_phi_access(role_of(x_role)) else rt.pack.mask_outbox(rt, rows)

    # ---------------- audit ----------------
    @app.get("/api/audit")
    def audit(request: Request, case_id: str | None = None, limit: int = 400) -> list[dict]:
        return rt_of(request).audit.blocks(case_id, limit)

    @app.post("/api/audit/verify")
    def verify(request: Request, x_role: str | None = Header(None)) -> dict:
        rt = rt_of(request)
        rt.rbac.require(role_of(x_role), "verify_audit")
        return rt.audit.verify()

    @app.post("/api/audit/tamper")
    def tamper(request: Request, body: TamperBody | None = None, x_role: str | None = Header(None), x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        params = {"block_id": body.block_id if body else None}
        return rt.gateway.execute("tamper_test", case_id=None, role=role, actor=actor_of(role, x_actor), params=params)

    @app.post("/api/audit/restore")
    def restore(request: Request, x_role: str | None = Header(None), x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        return rt.gateway.execute("audit_restore", case_id=None, role=role, actor=actor_of(role, x_actor), permission="tamper_test")

    # ---------------- dashboard + admin ----------------
    @app.get("/api/dashboard")
    def dashboard(request: Request) -> dict:
        rt = rt_of(request)
        return rt.pack.dashboard(rt)

    @app.post("/api/admin/autonomy")
    def set_autonomy(body: AutonomyBody, request: Request, x_role: str | None = Header(None), x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        if body.level not in (1, 2, 3):
            raise HTTPException(400, "autonomy level must be 1, 2 or 3")
        return rt.gateway.execute("set_autonomy", case_id=None, role=role, actor=actor_of(role, x_actor), params={"level": body.level})

    @app.post("/api/admin/kill-switch")
    def kill_switch(body: KillSwitchBody, request: Request, x_role: str | None = Header(None), x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        return rt.gateway.execute("kill_switch", case_id=None, role=role, actor=actor_of(role, x_actor), params={"engaged": body.engaged})

    @app.post("/api/demo/reset")
    def reset(request: Request, body: ResetBody | None = None, x_role: str | None = Header(None)) -> dict:
        rt = rt_of(request)
        rt.rbac.require(role_of(x_role), "reset_demo")
        with rt.feed.busy:  # no live-feed tick or replay step runs while the desk is wiped
            for _ in range(50):  # let any in-flight case finish before wiping state
                if not any(rt.orchestrator.is_running(c["case_id"]) for c in rt.cases.list_light()):
                    break
                time.sleep(0.2)
            out = rt.pack.seed(rt, history=body.history if body else True)
            if rt.feed.running and rt.settings.day_backfill:
                rt.feed.start_day()
            return out

    # ---------------- live feed ----------------
    @app.get("/api/feed")
    def feed_status(request: Request) -> dict:
        return rt_of(request).feed.status()

    @app.post("/api/feed")
    def feed_toggle(body: FeedBody, request: Request, x_role: str | None = Header(None), x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        return rt.gateway.execute("set_feed", case_id=None, role=role, actor=actor_of(role, x_actor), params={"enabled": body.enabled})

    @app.get("/api/day")
    def day_status(request: Request) -> dict:
        return rt_of(request).feed.status()["day"] or {"active": False}

    @app.post("/api/day/replay")
    def day_replay(request: Request, body: DayBody | None = None, x_role: str | None = Header(None), x_actor: str | None = Header(None)) -> dict:
        rt, role = rt_of(request), role_of(x_role)
        if body and body.minutes is not None and not 0.5 <= body.minutes <= 60:
            raise HTTPException(400, "replay length must be between 0.5 and 60 minutes")
        return rt.gateway.execute("replay_day", case_id=None, role=role, actor=actor_of(role, x_actor),
                                  params={"minutes": body.minutes if body else None})

    @app.get("/api/notifications")
    def notifications(request: Request, after: int = 0, limit: int = 50) -> list[dict]:
        rt = rt_of(request)
        if after <= 0:
            return rt.events.notifications(max(0, rt.events.last_notification_id() - limit), limit)
        return rt.events.notifications(after, limit)

    @app.get("/api/stream")
    async def desk_stream(request: Request, after: int | None = None, last_event_id: str | None = Header(None)) -> StreamingResponse:
        rt = rt_of(request)
        start = after if after is not None else (int(last_event_id) if last_event_id and last_event_id.isdigit() else None)
        return StreamingResponse(rt.events.desk_stream(start), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
