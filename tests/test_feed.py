"""Live feed: synthetic arrivals processed on arrival, the simulated desk, retention and the desk stream."""
import asyncio
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.guard import screen
from core.orchestrator import Runtime
from core.settings import get_settings
from packs.health_preauth import actions, tools
from packs.health_preauth.generator import generate_request, static_history

TODAY = date(2026, 10, 24)


@pytest.fixture
def rt(tmp_path):
    runtime = Runtime(replace(get_settings(), llm_mode="mock", db_path=tmp_path / "f.db", live_feed=False, feed_max_cases=4))
    actions.seed_demo(runtime, history=False)
    return runtime


def test_generator_is_deterministic_and_valid():
    a, b = generate_request(2001, admission=TODAY), generate_request(2001, admission=TODAY)
    assert a == b and a["case_id"] == "PA-2001" and a["source"] == "feed"
    for seq in range(2001, 2061):
        req = generate_request(seq, admission=TODAY)
        form = req["form"]
        assert tools.policy_by_id(form["policy_number"]) and tools.lookup_procedure(form["proposed_procedure"], form["icd10"])["candidates"]
        assert sum(form["estimate"].values()) > 0 and form["room_rent_per_day"] > 0
        assert set(req.get("upload_on_query", [])).isdisjoint(req["attached_docs"])


def test_injections_only_where_planted():
    clean = generate_request(2001, admission=TODAY, force_injection=False)
    attacked = generate_request(2001, admission=TODAY, force_injection=True)
    assert not screen(clean["admission_note"]).detected
    assert screen(attacked["admission_note"]).detected


def test_committed_history_matches_the_generator():
    for req in static_history():
        assert tools.load_case(req["case_id"]) == req, f"{req['case_id']} is stale: run python scripts/make_dataset.py"


def test_arrival_is_processed_and_desk_settles_it(rt):
    out = rt.feed.tick(arrive=True)
    cid = out["arrived"]
    assert cid == "PA-2001"
    case = rt.cases.get(cid)
    assert case["status"] in ("AWAITING_APPROVAL", "ESCALATED", "QUERY_RAISED") and case["state"]["decision"]
    kinds = [n["kind"] for n in rt.events.notifications(0)]
    assert "arrived" in kinds and "settled" in kinds
    for _ in range(3):
        rt.feed.tick(arrive=False, desk_now=True)
    case = rt.cases.get(cid)
    assert case["decided_at"] and case["status"] in ("APPROVED", "PARTIALLY_APPROVED", "DENIED")
    assert case["state"]["human_decision"]["actor"].startswith("sim:")
    assert rt.audit.verify()["valid"]


def test_desk_never_touches_hero_cases(rt):
    rt.orchestrator.run("PA-1001")
    rt.feed.tick(arrive=True)
    rt.feed.tick(arrive=False, desk_now=True)
    assert rt.cases.get("PA-1001")["status"] == "AWAITING_APPROVAL"


def test_retention_keeps_the_newest_feed_cases(rt):
    for _ in range(7):
        rt.feed.tick(arrive=True)
        for _ in range(3):
            rt.feed.tick(arrive=False, desk_now=True)
    feed_ids = [r["case_id"] for r in rt.store.query("SELECT case_id FROM cases WHERE case_id LIKE 'PA-2%'")]
    assert len(feed_ids) <= 4 and "PA-2007" in feed_ids and "PA-2001" not in feed_ids
    assert rt.audit.verify()["valid"]  # pruning never rewrites the chain


def test_desk_stream_emits_feed_events(rt):
    rt.feed.tick(arrive=True)

    async def collect():
        chunks = []
        async for chunk in rt.events.desk_stream(after=0, max_seconds=1.0):
            chunks.append(chunk)
        return "".join(chunks)

    body = asyncio.run(collect())
    assert "event: feed" in body and '"kind": "arrived"' in body and "PA-2001" in body


def test_feed_api_is_rbac_gated(tmp_path):
    settings = replace(get_settings(), llm_mode="mock", db_path=tmp_path / "api.db", seed_history=False, live_feed=False)
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/feed").json()["enabled"] is True
        assert c.post("/api/feed", json={"enabled": False}, headers={"X-Role": "AUDITOR"}).status_code == 403
        r = c.post("/api/feed", json={"enabled": False}, headers={"X-Role": "PROCESSOR"})
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert c.get("/api/meta").json()["feed"]["enabled"] is False
        kinds = [n["kind"] for n in c.get("/api/notifications").json()]
        assert "set_feed" in kinds and "blocked" in kinds
        assert c.get("/api/health").json()["cases"] == 40
