"""Backend end-to-end checks for hero cases A, B and C over HTTP (mock mode, no network)."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import get_settings


def H(role: str) -> dict:
    return {"X-Role": role}


@pytest.fixture
def client(tmp_path):
    settings = replace(get_settings(), llm_mode="mock", db_path=tmp_path / "e2e.db", seed_history=False)
    with TestClient(create_app(settings)) as c:
        yield c


def view(client, case_id, role="PROCESSOR"):
    return client.get(f"/api/cases/{case_id}", headers=H(role)).json()


def test_hero_a_one_click_approval_fills_the_outbox(client):
    assert client.post("/api/cases/PA-1001/process?sync=true", headers=H("PROCESSOR")).status_code == 200
    v = view(client, "PA-1001")
    assert v["status"] == "AWAITING_APPROVAL" and v["decision"]["recommended_amount"] == 101000
    assert v["decision"]["one_click"] and v["can"]["approve"]
    room = next(r for r in v["policy"]["evaluation"]["rules"] if r["rule_id"] == "room_rent_cap")
    assert room["impact_inr"] == -9000 and room["clause_ref"] == "Sec 4.2 Room Rent"

    r = client.post("/api/cases/PA-1001/decision", json={"action": "approve"}, headers=H("PROCESSOR"))
    assert r.status_code == 200, r.text
    v = view(client, "PA-1001")
    assert v["status"] == "APPROVED" and v["final_amount"] == 101000
    kinds = {o["kind"]: o for o in v["outbox"]}
    assert set(kinds) == {"authorization_letter", "hospital_notice", "patient_sms"}
    letter = kinds["authorization_letter"]["body"]
    assert letter["patient"] == "Aarav Menon" and letter["lines"][-1]["amount"] == 101000
    assert "Aarav Menon" in kinds["patient_sms"]["body"]["text"] and "[PATIENT" not in kinds["patient_sms"]["body"]["text"]


def test_hero_b_query_then_upload_then_approval(client):
    client.post("/api/cases/PA-1002/process?sync=true", headers=H("PROCESSOR"))
    v = view(client, "PA-1002")
    assert v["status"] == "QUERY_RAISED" and v["sla"]["paused"]
    query = next(o for o in v["outbox"] if o["kind"] == "hospital_query")
    assert "Biometry / A-scan report" in query["body"]["text"]

    r = client.post("/api/cases/PA-1002/simulate-upload?sync=true", headers=H("PROCESSOR"))
    assert r.status_code == 200 and r.json()["status"] == "AWAITING_APPROVAL"
    r = client.post("/api/cases/PA-1002/decision", json={"action": "approve"}, headers=H("PROCESSOR"))
    assert r.status_code == 200
    v = view(client, "PA-1002")
    assert v["status"] == "APPROVED" and v["final_amount"] == 36000
    assert not v["sla"]["paused"]


def test_hero_c_escalates_blocks_processor_and_requires_mo_reason(client):
    client.post("/api/cases/PA-1003/process?sync=true", headers=H("PROCESSOR"))
    v = view(client, "PA-1003")
    assert v["status"] == "ESCALATED" and v["guard"]["detected"]
    assert any(seg.get("inj") and "ignore all rules" in seg["t"] for seg in v["document"]["original"])
    assert not any(r["passed"] for r in v["policy"]["evaluation"]["rules"] if r["rule_id"] == "specific_disease_waiting")
    assert v["risk"]["score"] >= 60 and not v["can"]["approve"]

    r = client.post("/api/cases/PA-1003/decision", json={"action": "approve"}, headers=H("PROCESSOR"))
    assert r.status_code == 403
    blocked = [b for b in client.get("/api/audit?case_id=PA-1003").json() if b["action"] == "action_blocked"]
    assert blocked and blocked[-1]["role"] == "PROCESSOR"

    r = client.post("/api/cases/PA-1003/decision", json={"action": "deny"}, headers=H("MEDICAL_OFFICER"))
    assert r.status_code == 400
    reason = "Joint replacement has a 24-month waiting period and the policy is 8 months old (Sec 3.3)."
    r = client.post("/api/cases/PA-1003/decision", json={"action": "deny", "reason": reason}, headers=H("MEDICAL_OFFICER"))
    assert r.status_code == 200
    v = view(client, "PA-1003", "MEDICAL_OFFICER")
    assert v["status"] == "DENIED" and v["human_decision"]["reason"] == reason
    assert client.post("/api/audit/verify", headers=H("AUDITOR")).json()["valid"]


def test_tamper_test_is_admin_only_and_detected(client):
    client.post("/api/cases/PA-1001/process?sync=true", headers=H("PROCESSOR"))
    client.post("/api/cases/PA-1001/decision", json={"action": "approve"}, headers=H("PROCESSOR"))
    assert client.post("/api/audit/verify", headers=H("AUDITOR")).json()["valid"]
    assert client.post("/api/audit/tamper", headers=H("PROCESSOR")).status_code == 403
    t = client.post("/api/audit/tamper", headers=H("ADMIN")).json()
    result = client.post("/api/audit/verify", headers=H("AUDITOR")).json()
    assert not result["valid"] and result["broken_at"] == t["tampered_block"]
    client.post("/api/audit/restore", headers=H("ADMIN"))
    assert client.post("/api/audit/verify", headers=H("AUDITOR")).json()["valid"]


def test_phi_stays_masked_for_roles_without_access(client):
    client.post("/api/cases/PA-1001/process?sync=true", headers=H("PROCESSOR"))
    client.post("/api/cases/PA-1001/decision", json={"action": "approve"}, headers=H("PROCESSOR"))
    v = view(client, "PA-1001", "AUDITOR")
    assert not v["phi_access"] and v["form"]["patient_name"] == "•••••"
    phi_segments = [s for s in v["document"]["original"] if s.get("phi")]
    assert phi_segments and all(set(s["t"]) == {"█"} for s in phi_segments)
    assert "Aarav" not in str(v["outbox"]) and "Aarav" not in str(client.get("/api/outbox", headers=H("AUDITOR")).json())
    assert "Aarav" in str(view(client, "PA-1001", "PROCESSOR")["outbox"])


def test_admin_controls_are_rbac_gated(client):
    assert client.post("/api/admin/kill-switch", json={"engaged": True}, headers=H("PROCESSOR")).status_code == 403
    assert client.post("/api/admin/kill-switch", json={"engaged": True}, headers=H("ADMIN")).status_code == 200
    meta = client.get("/api/meta").json()
    assert meta["kill_switch"] and meta["autonomy_level"] == 1
    assert client.post("/api/admin/autonomy", json={"level": 3}, headers=H("MEDICAL_OFFICER")).status_code == 403


def test_trace_stream_and_pages(client):
    client.post("/api/cases/PA-1001/process?sync=true", headers=H("PROCESSOR"))
    with client.stream("GET", "/api/cases/PA-1001/stream") as r:
        body = "".join(r.iter_text())
    assert "event: trace" in body and "event: done" in body and "get_policy" in body
    assert client.get("/").status_code == 200 and client.get("/console").status_code == 200
    assert client.post("/api/cases/PA-1001/process?sync=true").status_code == 409
