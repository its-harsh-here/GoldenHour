"""Action Agent, Reporter and case views for the health pre-auth pack.

Every side effect (letters, hospital queries, patient SMS, status changes) is an Action Gateway
handler, so RBAC and the audit chain apply uniformly. Drafts are written by the LLM in redacted form
and rehydrated locally; a deterministic template is used if a draft fails validation.
"""
from __future__ import annotations

import hashlib
import json
import re
import random
import time
from datetime import datetime, timedelta, timezone
from statistics import median

from pydantic import BaseModel

from core import clock
from core.guard import QUARANTINE_TOKEN, find_injection_spans
from core.llm import LLMError
from core.orchestrator import sla_info
from core.redaction import rehydrate
from packs.health_preauth import tools
from packs.health_preauth.agents import prompt, rupee_figures
from packs.health_preauth.generator import generate_request
from packs.health_preauth.rules import inr

INSURER = "Kestrel Mutual Health"
INSURER_NOTE = "Synthetic insurer for demonstration"
DESK = "GoldenHour Cashless Desk"
DESK_EMAIL = "cashless.desk@kestrel-mutual.example"
TOKEN_RE = re.compile(r"\[[A-Z]+_\d+\]")
PHI_FORM_FIELDS = ("patient_name", "patient_phone", "patient_email", "aadhaar", "policy_number", "treating_doctor")


class DecisionError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class Drafts(BaseModel):
    hospital_notice: str
    patient_sms: str


class QueryDraft(BaseModel):
    subject: str
    body: str


# ---------------------------------------------------------------- helpers

def _phi(case: dict) -> dict:
    return case["state"].get("intake", {}).get("phi_map", {})


def _patient_token(case: dict) -> str:
    return next((t for t in _phi(case) if t.startswith("[PATIENT")), "[PATIENT_1]")


def _tokenize(text: str, mapping: dict) -> str:
    for token, value in sorted(mapping.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = re.sub(re.escape(value), token, text, flags=re.IGNORECASE)
    return text


def _ref(case: dict, kind: str) -> str:
    return f"GH/{kind}/{case['case_id']}/{case['request']['form']['admission_date'].replace('-', '')}"


def _model_label(model: str, source: str) -> str:
    return model if source in ("live", "cache", "replay") else f"{source.split(' ')[0]}:{model}"


def _draft(rt, facts: dict, schema, fallback: dict, force_mock: bool) -> tuple[dict, dict]:
    """Ask the Action Agent for a draft; reject it if it invents figures or tokens."""
    text = json.dumps(facts, ensure_ascii=False)
    meta = {"model": _model_label("fast", "template"), "tokens": 0, "latency_ms": 0, "source": "template"}
    try:
        res = rt.llm.run_agent(agent="action", model="fast", system=prompt("action"), user=text,
                               schema=schema, mock=lambda _m: {"content": fallback}, force_mock=force_mock)
    except LLMError as err:
        meta["source"] = f"template (LLM unavailable: {str(err)[:80]})"
        return fallback, meta
    data = res.data.model_dump()
    meta.update(model=_model_label("fast", res.source), tokens=res.tokens, latency_ms=res.latency_ms, source=res.source)
    blob = " ".join(str(v) for v in data.values())
    if rupee_figures(blob) - rupee_figures(text) or set(TOKEN_RE.findall(blob)) - set(TOKEN_RE.findall(text)):
        meta["source"] = "template (draft rejected: unsupported figure or token)"
        return fallback, meta
    return data, meta


# ---------------------------------------------------------------- documents

def _letter(case: dict, kind: str, amount: int, role_label: str, actor: str, reason: str) -> dict:
    req, form = case["request"], case["request"]["form"]
    ev = case["state"].get("policy", {}).get("evaluation", {})
    denied = kind == "DENIED"
    lines = [{"label": "Amount requested (hospital estimate)", "amount": ev.get("claimed_amount", sum(form["estimate"].values()))}]
    conditions = []
    if not denied:
        for d in ev.get("deductions", []):
            lines.append({"label": f"Less: {d['label']} ({d['clause_ref']})", "amount": -d["amount"]})
        computed = ev.get("amount_before_blocking", amount)
        if amount != computed:
            lines.append({"label": "Medical Officer adjustment", "amount": amount - computed})
        lines.append({"label": "Amount authorized", "amount": amount, "total": True})
        conditions = ["Valid for admission within 15 days of this letter.",
                      "Final authorization at discharge is subject to the final bill and policy terms."]
        if ev.get("proportionate_deduction_risk"):
            conditions.append("Room rent exceeds the policy cap; proportionate deduction may apply to associated charges at the final bill.")
        if ev.get("failed_rules"):
            conditions.append("Authorized by Medical Officer exception: " + reason)
        conditions += ["Non-payable items as per the IRDAI list are borne by the insured.",
                       "This letter is an insurance authorization, not a clinical opinion."]
    else:
        conditions = ["The hospital may proceed on a cash basis.",
                      "The insured may file a reimbursement claim or seek a review through the grievance process."]
    return {
        "type": "denial" if denied else "authorization",
        "letterhead": INSURER, "letterhead_note": INSURER_NOTE, "desk": DESK,
        "reference": _ref(case, "DEN" if denied else "AUTH"),
        "date": time.strftime("%d %b %Y"),
        "to": {"name": req["hospital"]["name"], "city": req["hospital"]["city"], "code": req["hospital"]["hospital_code"], "attn": "TPA / Insurance Desk"},
        "patient": form["patient_name"], "policy_number": form["policy_number"], "procedure": form["proposed_procedure"],
        "diagnosis": form["provisional_diagnosis"], "admission_date": form["admission_date"], "room_category": form["room_category"],
        "lines": lines, "status": kind, "reason": reason, "conditions": conditions,
        "signed_by": {"role": role_label, "actor": actor}, "case_id": case["case_id"],
    }


def send_decision_documents(rt, case: dict, kind: str, amount: int, role: str, actor: str, reason: str, force_mock: bool) -> list[int]:
    cid, form, hosp, mapping = case["case_id"], case["request"]["form"], case["request"]["hospital"], _phi(case)
    role_label = rt.rbac.roles.get(role, {}).get("label", role)
    letter = _letter(case, kind, amount, role_label, actor, reason)
    denied = kind == "DENIED"
    pt = _patient_token(case)
    ev = case["state"].get("policy", {}).get("evaluation", {})
    claimed = ev.get("claimed_amount", sum(form["estimate"].values()))
    facts = {"message_type": "denial" if denied else "approval", "case_reference": cid, "letter_reference": letter["reference"],
             "patient": pt, "hospital": hosp["name"], "procedure": form["proposed_procedure"], "requested_amount": inr(claimed)}
    if denied:
        facts["reason"] = _tokenize(reason, mapping)
        fallback = {
            "hospital_notice": f"Cashless pre-authorization for {pt} ({form['proposed_procedure']}) is not approved after Medical Officer review. "
                               f"Reason: {facts['reason']} The patient may proceed on a cash basis and file a reimbursement claim. Reference {letter['reference']}.",
            "patient_sms": f"Dear {pt}, your cashless request at {hosp['name']} could not be approved (ref {cid}). The hospital has the reason. "
                           f"You may file a reimbursement claim or ask for a review. - {INSURER}",
        }
    else:
        deductions = [f"{d['label']} {inr(d['amount'])} ({d['clause_ref']})" for d in ev.get("deductions", [])]
        facts.update(authorized_amount=inr(amount), deductions=deductions)
        fallback = {
            "hospital_notice": f"Cashless pre-authorization {letter['reference']} for {pt} ({form['proposed_procedure']}) is approved for "
                               f"{inr(amount)} against {inr(claimed)} requested. "
                               + (f"Deductions: {'; '.join(deductions)}. " if deductions else "")
                               + "The authorization letter is attached; final settlement is subject to the discharge bill and policy terms.",
            "patient_sms": f"Dear {pt}, your cashless request at {hosp['name']} is approved for {inr(amount)} (ref {cid}). "
                           f"The hospital has the authorization letter. - {INSURER}",
        }
    drafts, meta = _draft(rt, facts, Drafts, fallback, force_mock)
    notice, sms = rehydrate(drafts["hospital_notice"], mapping), rehydrate(drafts["patient_sms"], mapping)
    gw, ids = rt.gateway, []
    letter_kind = "denial_letter" if denied else "authorization_letter"
    ids.append(gw.outbox_add(cid, letter_kind, "letter", hosp["tpa_email"], f"{'Pre-authorization decision' if denied else 'Cashless authorization'} {letter['reference']}", letter, actor))
    rt.events.emit(cid, "action", f"send_{letter_kind}", tool="action_gateway",
                   output={"reference": letter["reference"], "to": hosp["name"], "amount": inr(amount) if not denied else None})
    subject = f"{'Pre-authorization not approved' if denied else 'Pre-authorization approved'}: {cid}"
    ids.append(gw.outbox_add(cid, "hospital_notice", "email", hosp["tpa_email"], subject,
                             {"from": DESK_EMAIL, "to": hosp["tpa_email"], "subject": subject, "text": notice,
                              "attachments": [letter["reference"].replace("/", "-") + ".pdf"]}, actor))
    rt.events.emit(cid, "action", "notify_hospital", tool="action_gateway", model=meta["model"], tokens=meta["tokens"],
                   latency_ms=meta["latency_ms"], output={"channel": "email", "to": hosp["name"], "draft": meta["source"]})
    ids.append(gw.outbox_add(cid, "patient_sms", "sms", form["patient_phone"], "SMS", {"to": form["patient_phone"], "text": sms, "sender": "KESTRL"}, actor))
    rt.events.emit(cid, "action", "notify_patient", tool="action_gateway", output={"channel": "sms", "chars": len(sms)})
    return ids


# ---------------------------------------------------------------- gateway handlers

def _finalize(rt, case_id: str, params: dict, role: str, actor: str, kind: str, auto: bool = False) -> dict:
    case = rt.cases.get(case_id)
    amount = 0 if kind == "DENIED" else int(params.get("amount", 0))
    reason = params.get("reason", "")
    ids = send_decision_documents(rt, case, kind, amount, role, actor, reason, params.get("force_mock", False))
    override = bool(params.get("override"))
    rt.cases.update(case_id, status=kind, final_amount=amount, decided_at=clock.now(), assigned_role=None,
                    override=int(override), auto=int(auto))
    state = case["state"]
    state["human_decision"] = {"kind": kind, "amount": amount, "reason": reason, "role": role, "actor": actor,
                               "override": override, "auto": auto, "ts": clock.now()}
    if auto:
        pct = rt.pack.config["decision_policy"]["post_audit_sample_pct"]
        state["post_audit_sampled"] = int(hashlib.sha256(case_id.encode()).hexdigest(), 16) % 100 < pct
    rt.cases.save_state(case_id, state)
    rt.events.emit(case_id, "action", "status_update", tool="action_gateway",
                   output={"status": kind, "amount": inr(amount), "by": rt.rbac.roles.get(role, {}).get("label", role), "override": override})
    return {"status": kind, "amount": amount, "outbox_ids": ids,
            "audit": {"status": kind, "amount": amount, "override": override, "reason": reason[:200], "auto": auto}}


def apply_autonomy(rt, level: int) -> dict:
    """Level 3 covers the waiting queue too: clean cases at or below the threshold that wait for a Processor
    auto-execute now, exactly as they would have on arrival (10% sampled for post-audit). Levels 1 and 2 change
    only how the sign-off is offered, so they leave the queue alone."""
    if level < 3:
        return {"auto_executed": [], "post_audit_sampled": 0}
    threshold = rt.pack.config["decision_policy"]["auto_threshold_inr"]
    done, sampled = [], 0
    with rt.feed.busy:  # the simulated desk must not sign the same case at the same moment
        rows = rt.store.query("SELECT case_id, recommended_amount, hero FROM cases WHERE status='AWAITING_APPROVAL' "
                              "AND recommendation='RECOMMEND_APPROVE' AND recommended_amount <= ? ORDER BY received_at", (threshold,))
        for row in rows:
            cid = row["case_id"]
            if rt.orchestrator.is_running(cid) or rt.cases.status(cid) != "AWAITING_APPROVAL":
                continue
            rt.events.emit(cid, "action", "auto_execute", tool="action_gateway",
                           output={"level": 3, "amount": inr(row["recommended_amount"]), "queue": "waiting for a Processor"})
            # hero cases keep the server's LLM mode (recorded drafts in replay); the rest stay on mock, like the feed
            rt.gateway.execute("auto_approve", case_id=cid, role="SYSTEM", actor="agent:action",
                               params={"amount": row["recommended_amount"], "force_mock": not row["hero"]})
            done.append(cid)
            sampled += bool(rt.cases.get(cid)["state"].get("post_audit_sampled"))
    return {"auto_executed": done, "post_audit_sampled": sampled}


def handle_query(rt, case_id: str, params: dict, role: str, actor: str) -> dict:
    case = rt.cases.get(case_id)
    form, hosp, mapping = case["request"]["form"], case["request"]["hospital"], _phi(case)
    missing = params.get("missing") or case["state"].get("decision", {}).get("missing_docs", [])
    labels = [tools.doc_label(d) for d in missing]
    pt = _patient_token(case)
    facts = {"message_type": "query", "case_reference": case_id, "patient": pt, "hospital": hosp["name"],
             "procedure": form["proposed_procedure"], "missing_documents": labels,
             "sla": "decision clock paused until the documents arrive, then a decision within the remaining window"}
    fallback = {
        "subject": f"Query on cashless request {case_id}: {len(labels)} document(s) needed",
        "body": f"Dear TPA desk,\n\nTo complete the cashless pre-authorization for {pt} ({form['proposed_procedure']}), please upload:\n"
                + "\n".join(f"- {label}" for label in labels)
                + f"\n\nCase reference {case_id}. Our 60-minute decision clock is paused until the documents arrive and resumes on upload.\n\n{DESK}",
    }
    draft, meta = _draft(rt, facts, QueryDraft, fallback, params.get("force_mock", False))
    subject, text = rehydrate(draft["subject"], mapping), rehydrate(draft["body"], mapping)
    body = {"from": DESK_EMAIL, "to": hosp["tpa_email"], "subject": subject, "text": text, "reference": _ref(case, "QRY"),
            "missing": [{"code": c, "label": tools.doc_label(c)} for c in missing]}
    rt.gateway.outbox_add(case_id, "hospital_query", "email", hosp["tpa_email"], subject, body, actor)
    rt.cases.update(case_id, status="QUERY_RAISED", assigned_role="HOSPITAL", sla_paused_at=clock.now())
    rt.events.emit(case_id, "action", "send_hospital_query", tool="action_gateway", model=meta["model"], tokens=meta["tokens"],
                   latency_ms=meta["latency_ms"], status="paused",
                   output={"to": hosp["name"], "missing": labels, "sla": "paused", "draft": meta["source"]})
    return {"status": "QUERY_RAISED", "missing": missing, "audit": {"missing": missing, "to": hosp["name"]}}


def handle_escalate(rt, case_id: str, params: dict, role: str, actor: str) -> dict:
    reasons = params.get("reasons") or ["Processor requested Medical Officer review"]
    by_human = role != "SYSTEM"
    rt.cases.update(case_id, status="ESCALATED", assigned_role="MEDICAL_OFFICER", **({"override": 1} if by_human else {}))
    if by_human:
        case = rt.cases.get(case_id)
        case["state"].setdefault("decision", {}).setdefault("reasons", []).append(f"Escalated by {role.title()}: {reasons[0]}")
        rt.cases.save_state(case_id, case["state"])
    rt.events.emit(case_id, "action", "escalate_to_medical_officer", tool="action_gateway", status="alert",
                   output={"queue": "Medical Officer", "reasons": reasons})
    return {"status": "ESCALATED", "audit": {"reasons": reasons[:5], "by_human": by_human}}


def handle_simulate_upload(rt, case_id: str, params: dict, role: str, actor: str) -> dict:
    case = rt.cases.get(case_id)
    if case["status"] != "QUERY_RAISED":
        raise DecisionError(409, f"case is {case['status']}; there is no open hospital query")
    req = case["request"]
    docs = req.get("upload_on_query") or case["state"].get("decision", {}).get("missing_docs", [])
    req["attached_docs"] = list(dict.fromkeys(req.get("attached_docs", []) + list(docs)))
    rt.cases.save_request(case_id, req)
    now = clock.now()
    paused_for = now - (case.get("sla_paused_at") or now)
    rt.cases.update(case_id, status="PROCESSING", sla_paused_at=None,
                    sla_paused_seconds=(case.get("sla_paused_seconds") or 0) + paused_for, assigned_role=None)
    rt.events.emit(case_id, "intake", "hospital_upload", tool="hospital_portal",
                   output={"received": [tools.doc_label(d) for d in docs], "sla": "resumed", "paused_for_s": round(paused_for, 1)})
    return {"status": "PROCESSING", "received": list(docs), "resume_at": case["state"].get("resume_at", "docs"),
            "audit": {"received": list(docs), "paused_for_s": round(paused_for, 1)}}


def register(rt) -> None:
    gw = rt.gateway
    gw.register("approve", lambda cid, p, role, actor: _finalize(rt, cid, p, role, actor, "APPROVED"))
    gw.register("partial_approve", lambda cid, p, role, actor: _finalize(rt, cid, p, role, actor, "PARTIALLY_APPROVED"))
    gw.register("auto_approve", lambda cid, p, role, actor: _finalize(rt, cid, p, role, actor, "APPROVED", auto=True))
    gw.register("deny", lambda cid, p, role, actor: _finalize(rt, cid, p, role, actor, "DENIED"))
    gw.register("escalate", lambda cid, p, role, actor: handle_escalate(rt, cid, p, role, actor))
    gw.register("query_hospital", lambda cid, p, role, actor: handle_query(rt, cid, p, role, actor))
    gw.register("simulate_upload", lambda cid, p, role, actor: handle_simulate_upload(rt, cid, p, role, actor))


# ---------------------------------------------------------------- human decisions

def human_decision(rt, case_id: str, role: str, actor: str, action: str, amount: int | None = None,
                   reason: str = "", force_mock: bool = False) -> dict:
    """Validate a human decision, then send it through the Action Gateway (RBAC first, then input checks)."""
    case = rt.cases.get(case_id)
    if case is None:
        raise DecisionError(404, f"unknown case {case_id}")
    status, decision = case["status"], case["state"].get("decision", {})
    if status not in ("AWAITING_APPROVAL", "ESCALATED"):
        raise DecisionError(409, f"case is {status}; there is nothing to decide")
    rec, claimed = int(decision.get("recommended_amount", 0)), int(decision.get("claimed_amount", 0))
    reason, escalated = (reason or "").strip(), status == "ESCALATED"
    params = {"reason": reason, "force_mock": force_mock}

    def gate(gw_action: str, permission: str) -> None:
        if not rt.rbac.can(role, permission):  # raises Forbidden (403) and audits the attempt
            rt.gateway.execute(gw_action, case_id=case_id, role=role, actor=actor, params=params, permission=permission)

    if action == "approve":
        amt = rec if amount is None else int(amount)
        permission = "approve_escalated" if escalated or amt > rec else "partial_approve" if amt < rec else "approve"
        gw_action = "partial_approve" if amt < rec else "approve"
        gate(gw_action, permission)
        if amt <= 0:
            raise DecisionError(400, "Amount must be positive. To refuse cover, record a denial with a reason.")
        if amt > claimed:
            raise DecisionError(400, f"Amount cannot exceed the {inr(claimed)} requested.")
        if amt != rec and not reason:
            raise DecisionError(400, "A written reason is required when the amount differs from the computed eligible amount.")
        return rt.gateway.execute(gw_action, case_id=case_id, role=role, actor=actor, permission=permission,
                                  params={**params, "amount": amt, "override": amt != rec})
    if action == "deny":
        gate("deny", "deny")
        if not reason:
            raise DecisionError(400, "A Medical Officer must record a written reason for a denial.")
        failed = case["state"].get("policy", {}).get("evaluation", {}).get("failed_rules", [])
        return rt.gateway.execute("deny", case_id=case_id, role=role, actor=actor, params={**params, "override": rec > 0 and not failed})
    if action == "escalate":
        if escalated:
            raise DecisionError(409, "case is already with the Medical Officer")
        return rt.gateway.execute("escalate", case_id=case_id, role=role, actor=actor,
                                  params={**params, "reasons": [reason or "Processor requested Medical Officer review"]})
    raise DecisionError(400, f"unknown action '{action}'")


# ---------------------------------------------------------------- views

def _mask_text(text: str, mapping: dict) -> str:
    for value in sorted(mapping.values(), key=len, reverse=True):
        if value and len(value) >= 3:
            text = re.sub(re.escape(value), "█" * min(len(value), 12), text, flags=re.IGNORECASE)
    return text


def _mask(obj, mapping: dict):
    if isinstance(obj, str):
        return _mask_text(obj, mapping)
    if isinstance(obj, list):
        return [_mask(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: _mask(v, mapping) for k, v in obj.items()}
    return obj


def segments(text: str, phi_spans: list[dict], inj_spans: list[dict], mask: bool) -> list[dict]:
    """Split a document into runs flagged as PHI (redaction bars) and/or injection (quarantine highlight)."""
    cuts = sorted({0, len(text), *(s["start"] for s in phi_spans), *(s["end"] for s in phi_spans),
                   *(s["start"] for s in inj_spans), *(s["end"] for s in inj_spans)})
    out: list[dict] = []
    for a, b in zip(cuts, cuts[1:]):
        if a >= b:
            continue
        phi = next((s for s in phi_spans if s["start"] <= a and b <= s["end"]), None)
        inj = any(s["start"] <= a and b <= s["end"] for s in inj_spans)
        seg = {"t": text[a:b]}
        if phi:
            seg.update(phi=phi["kind"], token=phi["token"])
            if mask:
                seg["t"] = "█" * len(seg["t"])
        if inj:
            seg["inj"] = True
        out.append(seg)
    return out


def _token_spans(text: str) -> list[dict]:
    return [{"start": m.start(), "end": m.end(), "kind": m.group()[1:-1].rsplit("_", 1)[0], "token": m.group()}
            for m in TOKEN_RE.finditer(text)]


def case_summary(rt, c: dict, role: str) -> dict:
    form, st = c["request"]["form"], c["state"]
    phi = rt.rbac.has_phi_access(role)
    source = c.get("source") or ("hero" if c["hero"] else c["request"].get("source", "history"))
    sim_now = rt.feed.case_clock(source)
    return {
        "case_id": c["case_id"], "title": c["title"], "hero": c["hero"], "status": c["status"], "assigned_role": c["assigned_role"],
        "hospital": c["request"]["hospital"]["name"], "city": c["request"]["hospital"]["city"],
        "patient": form["patient_name"] if phi else "Patient ••••", "age": form["age"], "gender": form["gender"],
        "requested_amount": sum(form["estimate"].values()), "recommended_amount": c["recommended_amount"],
        "final_amount": c["final_amount"], "risk_score": c["risk_score"], "recommendation": c["recommendation"],
        "received_at": c["received_at"], "decided_at": c["decided_at"], "sla": sla_info(c, rt.sla_minutes, sim_now),
        "clock_shift_s": round(time.time() - sim_now, 1) if sim_now else 0,
        "injection": bool(c.get("injection")), "seeded": bool(c["seeded"]),
        "running": rt.orchestrator.is_running(c["case_id"]), "override": bool(c["override"]),
        "source": source, "run_mode": c.get("run_mode"),
    }


def case_view(rt, c: dict, role: str) -> dict:
    st, req = c["state"], c["request"]
    phi = rt.rbac.has_phi_access(role)
    mapping = st.get("intake", {}).get("phi_map", {})
    intake = st.get("intake", {})
    document = None
    if intake:
        original = intake["note_text"]
        model_text = intake["model_text"]
        q = [{"start": m.start(), "end": m.end()} for m in re.finditer(re.escape(QUARANTINE_TOKEN), model_text)]
        document = {
            "source": "pdf" if req.get("admission_note_pdf") else "text",
            "filename": req.get("admission_note_pdf") or "admission_note.txt",
            "original": segments(original, intake["phi_spans"], find_injection_spans(original), mask=not phi),
            "model": segments(model_text, _token_spans(model_text), q, mask=False),
            "phi_counts": {k: sum(1 for s in intake["phi_spans"] if s["kind"] == k) for k in {s["kind"] for s in intake["phi_spans"]}},
        }
    form = dict(req["form"])
    if not phi:
        form.update({k: "•••••" for k in PHI_FORM_FIELDS if form.get(k)})
    status, decision = c["status"], st.get("decision", {})
    rec = decision.get("recommended_amount", 0)
    can = {
        "approve": status in ("AWAITING_APPROVAL", "ESCALATED") and rt.rbac.can(role, "approve_escalated" if status == "ESCALATED" else "approve"),
        "change_amount": status in ("AWAITING_APPROVAL", "ESCALATED") and rt.rbac.can(role, "partial_approve"),
        "deny": status in ("AWAITING_APPROVAL", "ESCALATED") and rt.rbac.can(role, "deny"),
        "escalate": status == "AWAITING_APPROVAL" and rt.rbac.can(role, "escalate"),
        "simulate_upload": status == "QUERY_RAISED" and rt.rbac.can(role, "simulate_upload"),
        "process": status == "NEW" and rt.rbac.can(role, "process_case"),
    }
    outbox = rt.gateway.outbox(c["case_id"])
    view = {
        **case_summary(rt, c, role),
        "hospital_detail": req["hospital"], "form": form, "phi_access": phi,
        "attached_docs": [{"code": d, "label": tools.doc_label(d)} for d in req.get("attached_docs", [])],
        "document": document,
        "guard": intake.get("guard"),
        "intake": {k: intake.get(k) for k in ("extraction", "confidence", "cross_check_issues", "llm_source", "form_redacted")} if intake else None,
        "policy": st.get("policy"), "docs": st.get("docs"), "risk": st.get("risk"),
        "decision": decision or None, "explanation": st.get("explanation"), "step_failure": st.get("step_failure"),
        "human_decision": st.get("human_decision"), "post_audit_sampled": st.get("post_audit_sampled"),
        "outbox": outbox if phi else _mask(outbox, mapping),
        "can": can, "recommended_amount": rec,
        "autonomy_level": rt.autonomy_level, "kill_switch": rt.kill_switch,
    }
    return view


# ---------------------------------------------------------------- reporter

IST = timezone(timedelta(hours=5, minutes=30))
IST_OFFSET_S = 19800


def _hourly(cases: list[dict], now: float, sla_m: int) -> dict:
    """The last 24 IST hours: arrivals per hour split by outcome, and the median turnaround of that hour."""
    last = int((now + IST_OFFSET_S) // 3600)
    buckets = {b: {"solved": 0, "breached": 0, "open": 0, "tat": []} for b in range(last - 23, last + 1)}
    for c in cases:
        b = int((c["received_at"] + IST_OFFSET_S) // 3600)
        if b not in buckets:
            continue
        if c["decided_at"]:
            info = sla_info(c, sla_m, now)
            buckets[b]["breached" if info["breached"] else "solved"] += 1
            buckets[b]["tat"].append(info["elapsed_s"] / 60)
        else:
            buckets[b]["open"] += 1
    keys = sorted(buckets)
    return {"labels": [f"{k % 24:02d}:00" for k in keys], "tz": "IST",
            "solved": [buckets[k]["solved"] for k in keys], "breached": [buckets[k]["breached"] for k in keys],
            "open": [buckets[k]["open"] for k in keys],
            "median_tat": [round(median(buckets[k]["tat"]), 1) if buckets[k]["tat"] else None for k in keys],
            "arrivals": sum(sum(buckets[k][f] for f in ("solved", "breached", "open")) for k in keys)}


def dashboard(rt) -> dict:
    roi, sla_m = rt.pack.config["roi"], rt.sla_minutes
    now = clock.now()
    cases = rt.cases.list_light()
    processed = [c for c in cases if c["recommendation"]]
    decided = sorted((c for c in cases if c.get("decided_at")), key=lambda c: c["decided_at"])
    tat = []
    for c in decided:
        info = sla_info(c, sla_m, now)
        tat.append({"case_id": c["case_id"], "title": c["title"], "minutes": round(info["elapsed_s"] / 60, 1),
                    "status": c["status"], "breached": info["breached"], "seeded": bool(c["seeded"]),
                    "source": c.get("source"), "decided_at": c["decided_at"]})
    minutes = [t["minutes"] for t in tat]
    statuses = [c["status"] for c in cases]
    reviewed = [c for c in cases if c.get("decided_at") or c["status"] == "ESCALATED"]
    humans = [c for c in reviewed if not c.get("auto")]
    overrides = sum(1 for c in humans if c["override"])
    saved_per_case = roi["manual_minutes_per_case"] - roi["assisted_minutes_per_case"]
    annual_hours = roi["daily_volume"] * roi["working_days_per_year"] * saved_per_case / 60
    count = lambda sql: rt.store.one(sql)["n"]  # noqa: E731
    return {
        "kpis": {
            "processed": len(processed), "decided": len(decided),
            "median_tat_min": round(median(minutes), 1) if minutes else None,
            "within_sla_pct": round(100 * sum(1 for t in tat if not t["breached"]) / len(tat)) if tat else None,
            "override_rate_pct": round(100 * overrides / len(humans)) if humans else 0,
            "overrides": overrides, "human_reviews": len(humans),
            "queries_sent": count("SELECT COUNT(*) AS n FROM outbox WHERE kind='hospital_query'"),
            "hours_saved": round(len(processed) * saved_per_case / 60, 1),
            "deductions_inr": sum(c.get("deductions_inr") or 0 for c in processed),
            "injections_blocked": sum(1 for c in processed if c.get("injection")),
            "unauthorized_blocked": count("SELECT COUNT(*) AS n FROM audit_log WHERE action='action_blocked'"),
        },
        "tat": tat[-30:],
        "today": _hourly(cases, now, sla_m),
        "split": {
            "Approved": statuses.count("APPROVED"), "Partially approved": statuses.count("PARTIALLY_APPROVED"),
            "Denied by MO": statuses.count("DENIED"), "With Medical Officer": statuses.count("ESCALATED"),
            "Query to hospital": statuses.count("QUERY_RAISED"), "Awaiting sign-off": statuses.count("AWAITING_APPROVAL"),
        },
        "roi": {**roi, "saved_minutes_per_case": saved_per_case, "annual_hours": round(annual_hours),
                "annual_inr": round(annual_hours * roi["processor_cost_per_hour_inr"])},
        "sla_minutes": sla_m,
        "baseline": {"label": "LocalCircles survey (Jan 2025): about 6 in 10 claimants waited 6-48 hours", "hours_low": 6, "hours_high": 48},
        "autonomy_level": rt.autonomy_level, "kill_switch": rt.kill_switch,
        "feed": rt.feed.status(),
    }


# ---------------------------------------------------------------- simulated desk (seeded history + live feed)

def _denial_reason(ev: dict) -> str:
    by_id = {r["rule_id"]: r for r in ev.get("rules", [])}
    parts = [f"{by_id[f]['label']} not met ({by_id[f]['clause_ref']}): {by_id[f]['explanation']}" for f in ev.get("failed_rules", []) if f in by_id]
    return " ".join(parts) or "Automated checks could not establish eligibility; cashless authorization not approved."


def desk_decision(rt, case_id: str, tag: str, force_mock: bool = True) -> str | None:
    """What a reviewer on the desk would do next with a pending case. Actors are labelled `<tag>:<role>`."""
    case = rt.cases.get(case_id)
    if case is None:
        return None
    status, st = case["status"], case["state"]
    if status == "QUERY_RAISED":
        out = rt.gateway.execute("simulate_upload", case_id=case_id, role="PROCESSOR", actor=f"{tag}:hospital")
        with rt.llm.paced():
            rt.orchestrator.run(case_id, from_step=out["resume_at"], force_mock=force_mock)
        return "upload"
    if status == "AWAITING_APPROVAL":
        human_decision(rt, case_id, "PROCESSOR", f"{tag}:processor", "approve", force_mock=force_mock)
        return "approve"
    if status != "ESCALATED":
        return None
    ev, docs = st.get("policy", {}).get("evaluation", {}), st.get("docs", {})
    rec = int(st.get("decision", {}).get("recommended_amount", 0))
    mo = f"{tag}:medical_officer"
    if ev.get("failed_rules") or rec <= 0:
        human_decision(rt, case_id, "MEDICAL_OFFICER", mo, "deny", reason=_denial_reason(ev), force_mock=force_mock)
        return "deny"
    bench = int(docs.get("benchmark_rate_inr") or 0)
    if docs.get("cost_deviation_pct", 0) > 25 and 0 < bench < rec:
        reason = (f"Estimate {docs['cost_deviation_pct']:.0f}% above the package benchmark; authorized at the benchmark of "
                  f"{inr(bench)}. Balance may be claimed at the final bill with invoices.")
        human_decision(rt, case_id, "MEDICAL_OFFICER", mo, "approve", amount=bench, reason=reason, force_mock=force_mock)
        return "partial"
    injected = st.get("intake", {}).get("guard", {}).get("detected")
    reason = ("Quarantined instruction text disregarded; documents and rule results verified." if injected
              else "High-value request reviewed against package norms and the attached documents.")
    human_decision(rt, case_id, "MEDICAL_OFFICER", mo, "approve", amount=rec, reason=reason, force_mock=force_mock)
    return "approve"


def brief(case: dict | None) -> dict:
    """A one-line summary of a case for desk-wide notifications (no PHI)."""
    if not case:
        return {}
    req = case["request"]
    return {"title": case["title"], "hospital": req["hospital"]["name"], "city": req["hospital"]["city"],
            "requested": sum(req["form"]["estimate"].values()), "status": case["status"],
            "recommendation": case.get("recommendation"), "recommended_amount": case.get("recommended_amount"),
            "final_amount": case.get("final_amount"), "source": case.get("source") or req.get("source", "history"),
            "injection": bool(case.get("injection"))}


GENERATED = ("feed", "day")
PENDING = ("AWAITING_APPROVAL", "ESCALATED", "QUERY_RAISED")


def _arrive(rt, seq: int, source: str, process: bool) -> str:
    """A generated hospital request arrives (at the business clock's time) as NEW; `process` runs the agents now."""
    now = clock.now()
    req = generate_request(seq, admission=datetime.fromtimestamp(now, IST).date(), source=source)
    cid = req["case_id"]
    rt.cases.create(cid, rt.pack.name, req, req["title"], None, now, source=source)
    rt.audit.append(case_id=cid, actor=f"hospital:{req['hospital']['hospital_code']}", role="HOSPITAL", action="case_received",
                    details={"title": req["title"], "hospital": req["hospital"]["name"], "source": source})
    rt.events.notify(cid, "arrived", brief(rt.cases.get(cid)))
    if process:  # the simulator, with model-like timing on the simulated clock
        with rt.llm.paced():
            rt.orchestrator.run(cid, force_mock=True)
    return cid


def feed_arrival(rt) -> str:
    """Real-time feed: a new request arrives now and waits as NEW until the feed hands it to the agents."""
    seq = int(rt.store.kv_get("feed_seq", 2000)) + 1
    rt.store.kv_set("feed_seq", seq)
    return _arrive(rt, seq, "feed", process=False)


def _desk_delay_s(case_id: str, status: str) -> int:
    return 60 + int(hashlib.sha256(f"{case_id}:{status}".encode()).hexdigest(), 16) % 360  # 1-7 minutes


def feed_desk(rt, force_mock: bool = True, force: bool = False) -> list[tuple[str, str | None]]:
    """Simulated desk staff (sim:processor, sim:medical_officer, sim:hospital) work the generated queue in real time."""
    now, acted = clock.now(), []
    pending = rt.store.query("SELECT case_id, status, updated_at, source FROM cases WHERE source IN ('feed', 'day') "
                             "AND status IN ('AWAITING_APPROVAL', 'ESCALATED', 'QUERY_RAISED') ORDER BY updated_at")
    for row in pending:
        if rt.orchestrator.is_running(row["case_id"]):
            continue
        # a case left open when a day replay ends keeps the timing that day planned for it
        delay = (_day_desk_minutes(row["case_id"], row["status"]) * 60 if row["source"] == "day"
                 else _desk_delay_s(row["case_id"], row["status"]))
        if not force and now - (row["updated_at"] or now) < delay:
            continue
        try:
            acted.append((row["case_id"], desk_decision(rt, row["case_id"], "sim", force_mock)))
        except Exception as err:  # one bad case must not stall the desk
            print(f"simulated desk skipped {row['case_id']}: {type(err).__name__}: {err}", flush=True)
    return acted


def _drop_cases(rt, case_ids: list[str]) -> None:
    for cid in case_ids:
        for table in ("cases", "trace_events", "outbox"):
            rt.store.execute(f"DELETE FROM {table} WHERE case_id=?", (cid,))


def feed_prune(rt, keep: int) -> int:
    """Keep the newest `keep` live-feed cases; older decided ones leave the inbox (the audit chain keeps them)."""
    feed = rt.store.query("SELECT case_id, decided_at FROM cases WHERE source='feed' ORDER BY received_at")
    drop = [r["case_id"] for r in feed if r["decided_at"] is not None][: max(0, len(feed) - keep)]
    _drop_cases(rt, drop)
    rt.store.execute("DELETE FROM notifications WHERE id < (SELECT MAX(id) - 3000 FROM notifications)")
    return len(drop)


# ---------------------------------------------------------------- day replay (24 hours on a simulated clock)

# Relative arrival rate by IST hour: pre-authorizations peak late morning and mid-afternoon.
DAY_PROFILE = [0.25, 0.2, 0.15, 0.15, 0.2, 0.3, 0.55, 0.9, 1.4, 1.85, 2.1, 2.05,
               1.85, 1.7, 1.8, 1.9, 1.85, 1.65, 1.4, 1.15, 0.95, 0.75, 0.55, 0.4]


def day_clear(rt) -> int:
    """Remove generated cases (live feed and an earlier day replay) before a new replay."""
    ids = [r["case_id"] for r in rt.store.query("SELECT case_id FROM cases WHERE source IN ('feed', 'day')")]
    _drop_cases(rt, ids)
    return len(ids)


def day_plan(rt, start: float, end: float, volume: int) -> list[tuple[float, str, int]]:
    """Random arrivals across the window, shaped by the IST intraday profile (deterministic per hour).

    Arrivals start at the first full IST hour of the window, so every one lands in the dashboard's 24 hourly bars.
    """
    rng = random.Random(f"goldenhour-day-{int(end // 3600)}")
    n = max(1, round(rng.gauss(volume, volume ** 0.5)))
    first_hour = (int((end + IST_OFFSET_S) // 3600) - 23) * 3600 - IST_OFFSET_S
    start = max(start, first_hour)
    hours = [start + i * 3600 for i in range(int((end - start) // 3600) + 1) if start + i * 3600 < end]
    span = {h: min(3600.0, end - h) for h in hours}  # the current hour is only partly over
    weights = [DAY_PROFILE[int((h + IST_OFFSET_S) // 3600) % 24] * span[h] / 3600 for h in hours]
    times = sorted(h + rng.random() * span[h] for h in rng.choices(hours, weights=weights, k=n))
    first = int(rt.store.kv_get("day_seq", 3000))
    rt.store.kv_set("day_seq", first + n)
    return [(ts, "arrive", first + i + 1) for i, ts in enumerate(times)]


def _day_desk_minutes(case_id: str, status: str) -> float:
    """How long the simulated desk takes to act on a day-replay case in this status (deterministic per case)."""
    rng = random.Random(f"{case_id}:{status}:desk")
    if status == "AWAITING_APPROVAL":
        return rng.uniform(3, 28)
    if status == "ESCALATED":
        return rng.uniform(62, 95) if rng.random() < 0.06 else rng.uniform(12, 52)
    return rng.uniform(15, 110)  # QUERY_RAISED: the hospital takes a while to upload (the SLA clock is paused meanwhile)


def _next_desk_event(rt, case_id: str) -> list[tuple[float, str, str]]:
    """When the (simulated) desk next acts on a pending case, in simulated time."""
    status = rt.cases.status(case_id)
    if status not in PENDING:
        return []
    return [(clock.now() + _day_desk_minutes(case_id, status) * 60, "desk", case_id)]


def day_event(rt, kind: str, ref) -> list[tuple[float, str, object]]:
    """Handle one simulated event; returns follow-up events (all timestamps on the simulated clock)."""
    if kind == "arrive":
        return _next_desk_event(rt, _arrive(rt, int(ref), "day", process=True))
    if kind == "desk":
        if rt.cases.status(ref) not in PENDING:
            return []
        if desk_decision(rt, ref, "sim", force_mock=True) == "upload":
            return _next_desk_event(rt, ref)
    return []


# ---------------------------------------------------------------- demo seed

def seed_demo(rt, history: bool = True) -> dict:
    """Reset the desk: every case in the inbox; non-hero cases pre-processed (mock) as earlier-today history."""
    rt.store.reset()
    now = time.time()
    requests = tools.load_cases()
    for req in requests:
        rt.cases.create(req["case_id"], rt.pack.name, req, req["title"], req.get("hero"), now - req.get("received_offset_min", 0) * 60)
    rt.audit.append(case_id=None, actor="system", role="SYSTEM", action="demo_reset", details={"cases": len(requests), "history": history})
    for req in requests:
        rt.audit.append(case_id=req["case_id"], actor=f"hospital:{req['hospital']['hospital_code']}", role="HOSPITAL",
                        action="case_received", details={"title": req["title"], "hospital": req["hospital"]["name"]})
    if history:
        for req in requests:
            outcome = req.get("seed_outcome")
            if not outcome:
                continue
            cid = req["case_id"]
            received = rt.cases.get(cid)["received_at"]
            with clock.at(received + 4), rt.llm.paced():  # earlier today: the agents' run, on its own timeline
                rt.orchestrator.run(cid, force_mock=True)
            with clock.at(min(received + (outcome.get("tat_min") or 8) * 60, now - 30)):  # the human's sign-off, later on
                if outcome.get("auto"):
                    for _ in range(3):  # a queried case resumes after the upload, then gets its decision
                        if desk_decision(rt, cid, "seed") != "upload":
                            break
                elif outcome.get("action"):
                    action = "deny" if outcome["action"] == "deny" else "approve"
                    human_decision(rt, cid, outcome["human"], f"seed:{outcome['human'].lower()}", action,
                                   amount=outcome.get("amount"), reason=outcome.get("reason", ""), force_mock=True)
            c = rt.cases.get(cid)
            if c["decided_at"] and outcome.get("tat_min"):
                rt.cases.update(cid, decided_at=c["received_at"] + outcome["tat_min"] * 60)
            rt.cases.update(cid, seeded=1)
    rt.events.notify(None, "reset", {"cases": len(requests)})
    return {"cases": len(requests), "history": history}
