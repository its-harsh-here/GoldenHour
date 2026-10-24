"""HTTP smoke test against a running GoldenHour server: hero cases A, B, C and the audit chain.

Usage:  python run.py            (in another terminal)
        python scripts/smoke_http.py [base_url]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, role: str = "PROCESSOR", body: dict | None = None) -> tuple[int, dict | list]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"X-Role": role, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def wait_for(case_id: str, statuses: set[str], timeout: float = 240) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        _, view = call("GET", f"/api/cases/{case_id}")
        if view["status"] in statuses and not view["running"]:
            return view
        time.sleep(0.5)
    raise SystemExit(f"{case_id} did not reach {statuses} in {timeout}s")


def rupees(n: int) -> str:
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + parts) + "," + tail
    return "₹" + s


def main() -> None:
    for _ in range(60):
        try:
            if call("GET", "/api/health")[0] == 200:
                break
        except OSError:
            time.sleep(0.5)
    status, meta = call("GET", "/api/meta")
    print(f"server {BASE}  llm_mode={meta['llm_mode']}  autonomy={meta['autonomy_level']}")
    print("reset:", call("POST", "/api/demo/reset", body={"history": True})[1])

    print("\n== Case A (PA-1001) clean appendectomy ==")
    t0 = time.time()
    print("process:", call("POST", "/api/cases/PA-1001/process")[1])
    v = wait_for("PA-1001", {"AWAITING_APPROVAL", "ESCALATED"})
    d = v["decision"]
    print(f"status={v['status']} outcome={d['outcome']} recommended={rupees(d['recommended_amount'])} one_click={d['one_click']} risk={v['risk']['score']}")
    for r in v["policy"]["evaluation"]["rules"]:
        if r["impact_inr"]:
            print(f"  deduction {r['rule_id']} {r['impact_inr']} [{r['clause_ref']}]")
    code, res = call("POST", "/api/cases/PA-1001/decision", "PROCESSOR", {"action": "approve"})
    print(f"Processor approve -> HTTP {code} status={res.get('status')} amount={rupees(res.get('amount', 0))}")
    _, v = call("GET", "/api/cases/PA-1001")
    print(f"final status={v['status']} end-to-end {time.time() - t0:.1f}s")
    for o in v["outbox"]:
        print(f"  outbox {o['kind']:<21} via {o['channel']:<6} to {o['recipient']}")
    sms = next(o for o in v["outbox"] if o["kind"] == "patient_sms")
    print("  SMS:", sms["body"]["text"])

    print("\n== Case B (PA-1002) cataract, biometry missing ==")
    call("POST", "/api/cases/PA-1002/process")
    v = wait_for("PA-1002", {"QUERY_RAISED", "AWAITING_APPROVAL", "ESCALATED"})
    q = next((o for o in v["outbox"] if o["kind"] == "hospital_query"), None)
    print(f"status={v['status']} sla_paused={v['sla']['paused']} missing={v['decision']['missing_docs']}")
    print("  query subject:", q["subject"] if q else None)
    code, res = call("POST", "/api/cases/PA-1002/simulate-upload")
    print(f"simulate upload -> HTTP {code} received={res.get('received')}")
    v = wait_for("PA-1002", {"AWAITING_APPROVAL", "ESCALATED"})
    print(f"after upload status={v['status']} recommended={rupees(v['decision']['recommended_amount'])}")
    code, res = call("POST", "/api/cases/PA-1002/decision", "PROCESSOR", {"action": "approve"})
    print(f"Processor approve -> HTTP {code} status={res.get('status')} amount={rupees(res.get('amount', 0))}")

    print("\n== Case C (PA-1003) knee replacement + hidden injection ==")
    call("POST", "/api/cases/PA-1003/process")
    v = wait_for("PA-1003", {"ESCALATED", "AWAITING_APPROVAL"})
    print(f"status={v['status']} assigned={v['assigned_role']} injection_detected={v['guard']['detected']} "
          f"guard={v['guard']['method']} score={v['guard']['score']} risk={(v.get('risk') or {}).get('score')}")
    if v.get("step_failure"):
        print("  step failure routed to a human:", v["step_failure"]["reason"][:160])
    print("  flagged:", [s["text"] for s in v["guard"]["sentences"]])
    print("  failed rules:", ((v.get("policy") or {}).get("evaluation") or {}).get("failed_rules"))
    print("  reasons:", v["decision"]["reasons"])
    code, res = call("POST", "/api/cases/PA-1003/decision", "PROCESSOR", {"action": "approve"})
    print(f"Processor approve attempt -> HTTP {code} {res}")
    reason = "Joint replacement has a 24-month waiting period (Sec 3.3); the policy is 8 months old. Injection text ignored."
    code, res = call("POST", "/api/cases/PA-1003/decision", "MEDICAL_OFFICER", {"action": "deny", "reason": reason})
    print(f"Medical Officer deny with reason -> HTTP {code} status={res.get('status')}")

    print("\n== Audit chain ==")
    _, ver = call("POST", "/api/audit/verify", "AUDITOR")
    print(f"verify -> valid={ver['valid']} checked={ver['checked']}")
    code, res = call("POST", "/api/audit/tamper", "PROCESSOR")
    print(f"tamper as PROCESSOR -> HTTP {code}")
    code, res = call("POST", "/api/audit/tamper", "ADMIN")
    print(f"tamper as ADMIN -> HTTP {code} {res}")
    _, ver = call("POST", "/api/audit/verify", "AUDITOR")
    print(f"verify after tamper -> valid={ver['valid']} broken_at={ver['broken_at']} reason={ver['reason']}")
    call("POST", "/api/audit/restore", "ADMIN")
    _, ver = call("POST", "/api/audit/verify", "AUDITOR")
    print(f"verify after restore -> valid={ver['valid']} checked={ver['checked']}")
    _, dash = call("GET", "/api/dashboard")
    print("\ndashboard kpis:", json.dumps(dash["kpis"]))


if __name__ == "__main__":
    main()
