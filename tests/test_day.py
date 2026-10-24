"""Day replay: the business clock, a simulated 24 hours of arrivals and desk work, and its API."""
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core import clock
from core.feed import DAY_S, DayReplay
from core.orchestrator import Runtime
from core.settings import get_settings
from packs.health_preauth import actions
from packs.health_preauth.actions import IST_OFFSET_S

END = 1_792_800_000.0  # a fixed "now" so the plan is deterministic


@pytest.fixture
def rt(tmp_path):
    runtime = Runtime(replace(get_settings(), llm_mode="mock", db_path=tmp_path / "d.db", live_feed=False, day_volume=30))
    actions.seed_demo(runtime, history=False)
    return runtime


def test_clock_is_real_time_unless_simulated():
    assert abs(clock.now() - time.time()) < 1 and not clock.simulated()
    seen = {}
    with clock.at(1000.0):
        assert clock.now() == 1000.0 and clock.simulated()
        with clock.at(2000.0):
            assert clock.now() == 2000.0
        assert clock.now() == 1000.0
        worker = threading.Thread(target=lambda: seen.update(other=clock.now()))
        worker.start()
        worker.join()
    assert abs(seen["other"] - time.time()) < 1  # other threads (API requests) keep real time
    assert not clock.simulated()


def test_paced_replay_maps_real_seconds_to_simulated_time():
    day = DayReplay(0.0, 1000.0, [(10.0, "arrive", 1), (900.0, "arrive", 2), (2000.0, "arrive", 3)], speed=10.0)
    assert day.arrivals_total == 3 and len(day.heap) == 2  # events after the window are dropped
    day.t0 = time.time() - 5
    assert 49 <= day.target() <= 52
    assert DayReplay(0.0, 1000.0, [], speed=None).target() == 1000.0  # backfill: as fast as possible


def test_day_replay_runs_a_whole_day_on_the_simulated_clock(rt):
    status = rt.feed.run_day(end=END)
    assert not status["active"] and status["progress"] == 1.0 and status["error"] is None
    assert status["arrivals_done"] == status["arrivals_total"] > 10
    day = [c for c in rt.cases.list_light() if c["source"] == "day"]
    assert len(day) == status["arrivals_total"]
    start = END - DAY_S
    for c in day:
        assert start <= c["received_at"] <= END
        if c["decided_at"]:
            assert c["received_at"] <= c["decided_at"] <= END + 2 * 3600
    decided = [c for c in day if c["decided_at"]]
    assert len(decided) >= 0.8 * len(day)  # the simulated desk clears most of the day
    assert {c["status"] for c in day} <= {"APPROVED", "PARTIALLY_APPROVED", "DENIED", "AWAITING_APPROVAL", "ESCALATED", "QUERY_RAISED"}
    kinds = [n["kind"] for n in rt.events.notifications(0, 5000)]
    assert kinds.count("day_started") == 1 and kinds.count("day_done") == 1 and "arrived" in kinds
    arrived = [n for n in rt.events.notifications(0, 5000) if n["kind"] == "arrived"]
    assert all(start <= n["ts"] <= END for n in arrived)  # notifications carry simulated times
    assert rt.audit.verify()["valid"]
    hero = rt.cases.get("PA-1001")
    assert hero["status"] == "NEW" and hero["received_at"] > END  # hero cases stay on real time, untouched


def test_replay_again_replaces_the_previous_day(rt):
    first = rt.feed.run_day(end=END)
    second = rt.feed.run_day(end=END + 3600)
    day = [c for c in rt.cases.list_light() if c["source"] == "day"]
    assert len(day) == second["arrivals_total"]
    assert min(int(c["case_id"].split("-")[1]) for c in day) > 3000 + first["arrivals_total"]


def test_dashboard_shows_the_day_hour_by_hour(rt):
    rt.feed.run_day(end=time.time())
    d = actions.dashboard(rt)
    today = d["today"]
    assert len(today["labels"]) == 24 and today["tz"] == "IST"
    day = [c for c in rt.cases.list_light() if c["source"] == "day"]
    assert len(day) <= today["arrivals"] <= len(rt.cases.list_light())  # plus the inbox cases received today
    assert sum(today["solved"]) + sum(today["breached"]) == sum(1 for c in day if c["decided_at"])
    assert d["kpis"]["processed"] >= len(day)


def test_open_replay_cases_run_on_the_replay_clock(rt):
    rt.feed.start_day(minutes=1, end=END)
    rt.feed.day.t0 = time.time() - 30  # half way through the simulated day
    rt.feed.day_step(budget_s=10)
    day = rt.feed.day
    assert day.status()["active"] and 0.4 < day.status()["progress"] < 0.6
    rows = [c for c in rt.cases.list_light() if c["source"] == "day"]
    assert rows
    for c in rows:
        summary = rt.pack.case_summary(rt, c, "PROCESSOR")
        assert summary["clock_shift_s"] > 0
        assert summary["sla"]["elapsed_s"] <= day.sim_now - c["received_at"] + 1


def test_light_list_skips_agent_state(rt):
    rt.orchestrator.run("PA-1001")
    light = {c["case_id"]: c for c in rt.cases.list_light()}
    full = rt.cases.get("PA-1001")
    assert full["state"] and light["PA-1001"]["state"] == {}
    assert light["PA-1001"]["request"] == full["request"] and light["PA-1001"]["status"] == full["status"]


def test_day_api(tmp_path):
    settings = replace(get_settings(), llm_mode="mock", db_path=tmp_path / "api.db", seed_history=False, live_feed=False, day_volume=20)
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/day").json() == {"active": False}
        assert c.get("/api/meta").json()["day_minutes"] == settings.day_replay_minutes
        assert c.post("/api/day/replay", json={"minutes": 1}, headers={"X-Role": "AUDITOR"}).status_code == 403
        assert c.post("/api/day/replay", json={"minutes": 0.1}, headers={"X-Role": "PROCESSOR"}).status_code == 400
        r = c.post("/api/day/replay", json={"minutes": 1}, headers={"X-Role": "PROCESSOR"})
        assert r.status_code == 200 and r.json()["active"] and r.json()["mode"] == "paced" and r.json()["minutes"] == 1.0
        rt = c.app.state.rt
        rt.feed.day.t0 = time.time() - 120  # past the end of the replay
        while rt.feed.day_active:
            rt.feed.day_step(budget_s=5)
        done = c.get("/api/day").json()
        assert not done["active"] and done["progress"] == 1.0 and done["arrivals_done"] == done["arrivals_total"]
        kinds = [n["kind"] for n in c.get("/api/notifications?limit=500").json()]
        assert "replay_day" in kinds and "day_done" in kinds


def test_live_mode_needs_an_endpoint_and_models(monkeypatch):
    for key in ("LLM_BASE_URL", "MODEL_FAST", "MODEL_REASON", "MODEL_GUARD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_MODE", "live")
    s = get_settings()
    assert s.llm_mode == "replay" and "LLM_BASE_URL, MODEL_FAST, MODEL_REASON, MODEL_GUARD" in s.llm_mode_note
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1")
    assert "missing MODEL_FAST, MODEL_REASON, MODEL_GUARD:" in get_settings().llm_mode_note  # names only what is missing
    monkeypatch.setenv("MODEL_FAST", "fast-model")
    monkeypatch.setenv("MODEL_REASON", "reason-model")
    monkeypatch.setenv("MODEL_GUARD", "guard-model")
    s = get_settings()
    assert s.llm_mode == "live" and not s.llm_mode_note
    assert (s.model_id("fast"), s.model_id("reason"), s.model_id("guard")) == ("fast-model", "reason-model", "guard-model")
    monkeypatch.setenv("LLM_MODE", "something-else")
    assert get_settings().llm_mode == "replay"


def test_cases_left_open_by_a_replay_keep_their_planned_timing(tmp_path):
    rt = Runtime(replace(get_settings(), llm_mode="mock", db_path=tmp_path / "late.db", live_feed=False, day_volume=200))
    actions.seed_demo(rt, history=False)
    # a full-size day ending at 11:59 IST, near the daily peak, so its last hour is still being worked
    end = (END + IST_OFFSET_S) // DAY_S * DAY_S - IST_OFFSET_S + 12 * 3600 - 60
    rt.feed.run_day(end=end)
    open_before = {c["case_id"] for c in rt.cases.list_light() if c["source"] == "day" and not c["decided_at"]}
    assert open_before
    with clock.at(end + 1):  # the live desk's first look, right after the replay
        first = rt.pack.feed_desk(rt, True)
    assert not [cid for cid, _ in first if cid in open_before]  # nothing decided early, all at once
    with clock.at(end + 3 * 3600):  # by then every planned sign-off has fallen due
        later = rt.pack.feed_desk(rt, True)
    assert {cid for cid, _ in later} & open_before
