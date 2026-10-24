from dataclasses import replace

import pytest

from core.llm import LLMError
from core.orchestrator import Runtime, sla_info
from core.settings import get_settings
from packs.health_preauth import actions


@pytest.fixture
def rt(tmp_path):
    runtime = Runtime(replace(get_settings(), llm_mode="mock", db_path=tmp_path / "o.db"))
    actions.seed_demo(runtime, history=False)
    return runtime


def test_case_a_waits_for_one_click_signoff(rt):
    rt.orchestrator.run("PA-1001")
    case = rt.cases.get("PA-1001")
    assert case["status"] == "AWAITING_APPROVAL" and case["assigned_role"] == "PROCESSOR"
    d = case["state"]["decision"]
    assert d["outcome"] == "RECOMMEND_APPROVE" and d["recommended_amount"] == 101000 and d["one_click"]
    agents = {e["agent"] for e in rt.events.list("PA-1001")}
    assert {"intake", "policy", "docs", "risk", "orchestrator", "action"} <= agents
    assert any(e["tool"] == "get_policy" for e in rt.events.list("PA-1001"))


def test_case_b_pauses_on_query_and_resumes(rt):
    rt.orchestrator.run("PA-1002")
    case = rt.cases.get("PA-1002")
    assert case["status"] == "QUERY_RAISED" and case["sla_paused_at"]
    assert sla_info(case, 60)["paused"]
    query = [o for o in rt.gateway.outbox("PA-1002") if o["kind"] == "hospital_query"]
    assert query and "Biometry / A-scan report" in query[0]["body"]["text"]
    out = rt.gateway.execute("simulate_upload", case_id="PA-1002", role="PROCESSOR", actor="t")
    rt.orchestrator.run("PA-1002", from_step=out["resume_at"])
    case = rt.cases.get("PA-1002")
    assert case["status"] == "AWAITING_APPROVAL" and case["sla_paused_at"] is None
    assert case["state"]["decision"]["recommended_amount"] == 36000


def test_case_c_escalates_with_reasons(rt):
    rt.orchestrator.run("PA-1003")
    case = rt.cases.get("PA-1003")
    assert case["status"] == "ESCALATED" and case["assigned_role"] == "MEDICAL_OFFICER"
    reasons = " ".join(case["state"]["decision"]["reasons"])
    assert "injection" in reasons and "Specified-disease waiting period" in reasons
    assert case["risk_score"] >= 60


def test_llm_failure_routes_to_a_human(rt, monkeypatch):
    original = rt.llm.run_agent

    def flaky(**kwargs):
        if kwargs["agent"] == "policy":
            raise LLMError("Groq rate limit (429) persisted after 4 attempts")
        return original(**kwargs)

    monkeypatch.setattr(rt.llm, "run_agent", flaky)
    rt.orchestrator.run("PA-1001")
    case = rt.cases.get("PA-1001")
    assert case["status"] == "ESCALATED"
    assert case["state"]["step_failure"]["step"] == "policy"
    assert any("429" in r for r in case["state"]["decision"]["reasons"])


def test_autonomy_level_3_auto_executes_clean_case(rt):
    rt.gateway.execute("set_autonomy", case_id=None, role="ADMIN", actor="t", params={"level": 3})
    rt.orchestrator.run("PA-1001")
    case = rt.cases.get("PA-1001")
    assert case["status"] == "APPROVED" and case["final_amount"] == 101000
    assert case["state"]["human_decision"]["auto"] is True
    assert {o["kind"] for o in rt.gateway.outbox("PA-1001")} == {"authorization_letter", "hospital_notice", "patient_sms"}


def test_kill_switch_forces_level_1(rt):
    rt.gateway.execute("set_autonomy", case_id=None, role="ADMIN", actor="t", params={"level": 3})
    rt.gateway.execute("kill_switch", case_id=None, role="ADMIN", actor="t", params={"engaged": True})
    assert rt.autonomy_level == 1
    rt.orchestrator.run("PA-1001")
    case = rt.cases.get("PA-1001")
    assert case["status"] == "AWAITING_APPROVAL" and not case["state"]["decision"]["one_click"]


def test_seeded_history_gives_dashboard_data(tmp_path):
    runtime = Runtime(replace(get_settings(), llm_mode="mock", db_path=tmp_path / "s.db"))
    actions.seed_demo(runtime, history=True)
    dash = actions.dashboard(runtime)
    assert dash["kpis"]["decided"] == 6 and dash["kpis"]["overrides"] == 1
    assert runtime.cases.get("PA-1010")["status"] == "AWAITING_APPROVAL"
    assert runtime.audit.verify()["valid"]
