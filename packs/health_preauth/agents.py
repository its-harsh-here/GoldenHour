"""Health pre-authorization agents.

Each agent is a function with its own system prompt (prompts/*.md) and an allow-list of tools.
Mock fixtures sit next to each agent: deterministic functions of the agent's own inputs, used in
mock mode, as the replay-miss fallback, and by the test suite.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from core.guard import screen
from core.llm import Tool
from core.orchestrator import StepContext
from core.redaction import Redactor
from packs.health_preauth import rules, tools
from packs.health_preauth.rules import ClaimInput, inr, months_between

PROMPTS = Path(__file__).resolve().parent / "prompts"
POLICY_PATTERN = ("POLICY", r"\bGH-POL-\d{6}\b")
PHI_FORM_FIELDS = ("patient_name", "patient_phone", "patient_email", "aadhaar", "policy_number", "treating_doctor")


def prompt(name: str, **values) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _to_int(value):
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        return int(float(cleaned)) if cleaned not in ("", "-", ".") else 0
    if isinstance(value, float):
        return int(round(value))
    return value


# ======================= Intake Agent =======================

class Estimate(BaseModel):
    room: int = 0
    ot: int = 0
    surgeon: int = 0
    consumables: int = 0
    medicines: int = 0
    investigations: int = 0

    _ints = field_validator("*", mode="before")(classmethod(lambda cls, v: _to_int(v) if v is not None else 0))


class IntakeExtraction(BaseModel):
    patient_ref: str = ""
    policy_ref: str
    admission_date: date
    provisional_diagnosis: str
    icd10_codes: list[str] = []
    proposed_procedure: str
    room_rent_per_day: int
    expected_los_days: int
    estimate: Estimate
    estimate_total_stated: int = 0
    is_accident: bool = False
    attached_docs: list[str] = []
    symptom_duration_months: int | None = None
    confidence: float = Field(ge=0, le=1)
    notes: str = ""

    _ints = field_validator("room_rent_per_day", "expected_los_days", "estimate_total_stated", "symptom_duration_months",
                            mode="before")(classmethod(lambda cls, v: _to_int(v) if v is not None else None))


def build_redactor(request: dict) -> Redactor:
    form = request["form"]
    patient = form.get("patient_name", "")
    entities = [("PATIENT", patient), ("DOCTOR", re.sub(r"^Dr\.?\s+", "", form.get("treating_doctor", "")))]
    policy = tools.policy_by_id(form.get("policy_number", ""))
    for member in (policy or {}).get("members", []):
        if member["name"].lower() != patient.lower():
            entities.append(("PERSON", member["name"]))
    return Redactor(entities, [POLICY_PATTERN])


def longest_duration_months(text: str) -> int | None:
    per_unit = {"year": 12, "month": 1, "week": 0.25, "day": 1 / 30}
    found = [int(n) * per_unit[u.lower()] for n, u in re.findall(r"(?:for|over|past)\s+(\d+)\s+(year|month|week|day)s?", text, re.I)]
    return int(max(found)) if found else None


def mock_intake(request: dict, redactor: Redactor, note: str) -> dict:
    form = request["form"]
    token = {v.lower(): k for k, v in redactor.mapping.items()}
    return {"content": {
        "patient_ref": token.get(form["patient_name"].lower(), "[PATIENT_1]"),
        "policy_ref": token.get(form["policy_number"].lower(), "[POLICY_1]"),
        "admission_date": form["admission_date"],
        "provisional_diagnosis": form["provisional_diagnosis"],
        "icd10_codes": [form["icd10"]],
        "proposed_procedure": form["proposed_procedure"],
        "room_rent_per_day": form["room_rent_per_day"],
        "expected_los_days": form["expected_los_days"],
        "estimate": form["estimate"],
        "estimate_total_stated": sum(form["estimate"].values()),
        "is_accident": form["is_accident"],
        "attached_docs": list(request.get("attached_docs", [])),
        "symptom_duration_months": longest_duration_months(note),
        "confidence": 0.94,
        "notes": "",
    }}


def crosscheck(ex: IntakeExtraction, request: dict) -> list[str]:
    """Deterministic comparison of the LLM's note extraction with the structured form."""
    form, issues = request["form"], []
    if form["icd10"].upper() not in [c.upper().strip() for c in ex.icd10_codes]:
        issues.append(f"ICD-10 {form['icd10']} on the form was not found in the note")
    if ex.admission_date.isoformat() != form["admission_date"]:
        issues.append("admission date differs between note and form")
    if ex.room_rent_per_day != form["room_rent_per_day"]:
        issues.append(f"room rent differs: note {inr(ex.room_rent_per_day)}, form {inr(form['room_rent_per_day'])}")
    if ex.expected_los_days != form["expected_los_days"]:
        issues.append("expected length of stay differs between note and form")
    diff = [k for k, v in form["estimate"].items() if getattr(ex.estimate, k, 0) != v]
    if diff:
        issues.append(f"estimate lines differ between note and form: {', '.join(diff)}")
    total = sum(form["estimate"].values())
    if ex.estimate_total_stated and ex.estimate_total_stated != total:
        issues.append(f"stated total {inr(ex.estimate_total_stated)} does not equal the sum of lines {inr(total)}")
    return issues


def intake_agent(ctx: StepContext) -> dict:
    """Redact PHI -> screen for prompt injection -> extract a structured case with confidence."""
    req = ctx.request
    t0 = time.perf_counter()
    text = tools.note_text(req)
    redactor = build_redactor(req)
    note = redactor.redact(text)
    form_view = {k: (redactor.redact_value(v) if k in PHI_FORM_FIELDS else v) for k, v in req["form"].items()}
    kinds: dict[str, int] = {}
    for span in note.spans:
        kinds[span.kind] = kinds.get(span.kind, 0) + 1
    ctx.emit("intake", "redact_phi", tool="redaction", input_summary=f"admission note, {len(text)} chars",
             output={"redacted": kinds, "tokens": sorted(redactor.mapping)}, latency_ms=_ms(t0))

    t0 = time.perf_counter()
    guard = screen(note.text, ctx.llm, ctx.settings.model_guard, force_mock=ctx.force_mock)
    ctx.emit("intake", "injection_screen", tool="prompt_guard", input_summary="redacted note",
             output={"detected": guard.detected, "score": guard.score, "method": guard.method,
                     "flagged": [s["text"] for s in guard.sentences], "error": guard.error},
             latency_ms=_ms(t0), model=guard.model or "regex", status="alert" if guard.detected else "ok")

    model_text = guard.quarantine(note.text)
    result = ctx.llm.run_agent(
        agent="intake", model=ctx.settings.model_fast,
        system=prompt("intake", doc_codes=", ".join(tools.doc_codes())),
        user=f"ADMISSION NOTE:\n{model_text}",
        schema=IntakeExtraction, mock=lambda _m: mock_intake(req, redactor, text), force_mock=ctx.force_mock,
    )
    ex: IntakeExtraction = result.data
    issues = crosscheck(ex, req)
    confidence = round(max(0.0, min(ex.confidence, 1 - 0.1 * len(issues))), 2)
    ctx.emit("intake", "extract_case", input_summary="quarantined, redacted note", model=ctx.model_label(ctx.settings.model_fast, result.source),
             tokens=result.tokens, latency_ms=result.latency_ms,
             output={"procedure": ex.proposed_procedure, "icd10": ex.icd10_codes, "llm_confidence": ex.confidence,
                     "confidence": confidence, "cross_check_issues": issues})
    return {"intake": {
        "note_text": text,
        "redacted_text": note.text,
        "phi_spans": note.as_dict()["spans"],
        "phi_map": redactor.mapping,
        "form_redacted": form_view,
        "guard": guard.as_dict(),
        "model_text": model_text,
        "extraction": ex.model_dump(mode="json"),
        "cross_check_issues": issues,
        "confidence": confidence,
        "llm_source": result.source,
    }}


# ======================= shared helpers =======================

RUPEE = re.compile(r"(?:₹|Rs\.?|INR)\s?(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|L\b)?", re.I)


def rupee_figures(text: str) -> set[int]:
    """Every rupee figure written in `text` (understands 1.01 lakh)."""
    values = set()
    for number, lakh in RUPEE.findall(text or ""):
        value = float(number.replace(",", "")) * (100000 if lakh else 1)
        values.add(int(round(value)))
    return values


def _resolver(ctx: StepContext):
    mapping = ctx.state["intake"]["phi_map"]
    return lambda token: mapping.get(token, token)


def _tool_hook(ctx: StepContext, agent: str):
    def hook(name: str, args: dict, out, ms: int) -> None:
        ctx.emit(agent, "tool_call", tool=name, input_summary=json.dumps(args, ensure_ascii=False)[:200], output=out, latency_ms=ms)
    return hook


def _tool_outputs(messages: list[dict]) -> list[dict]:
    outs = []
    for m in messages:
        if m.get("role") == "tool":
            try:
                outs.append(json.loads(m["content"]))
            except (TypeError, json.JSONDecodeError):
                pass
    return outs


def _admission(ctx: StepContext) -> date:
    return date.fromisoformat(ctx.request["form"]["admission_date"])


def resolve_procedure(form: dict) -> dict | None:
    hits = tools.lookup_procedure(f"{form['proposed_procedure']} {form['provisional_diagnosis']}", form["icd10"])["candidates"]
    return tools.procedure_by_key(hits[0]["procedure_key"]) if hits else None


def claim_input(form: dict) -> ClaimInput:
    """Money and codes for the rule engine come from the structured form, never from LLM output."""
    return ClaimInput(admission_date=form["admission_date"], is_accident=form["is_accident"], icd10_codes=[form["icd10"]],
                      room_rent_per_day=form["room_rent_per_day"], los_days=form["expected_los_days"], estimate=form["estimate"])


# ======================= Policy Agent =======================

class PolicyFindings(BaseModel):
    policy_found: bool
    ped_link_suspected: bool = False
    ped_link_reason: str = ""
    claim_history_note: str = ""
    coverage_notes: str = ""


POLICY_REF = {"type": "object", "properties": {"policy_ref": {"type": "string", "description": "policy token such as [POLICY_1]"}},
              "required": ["policy_ref"]}


def mock_policy(policy_ref: str):
    def fn(messages: list[dict]) -> dict:
        outs = _tool_outputs(messages)
        if not outs:
            return {"tool_calls": [{"name": "get_policy", "arguments": {"policy_ref": policy_ref}},
                                   {"name": "get_claim_history", "arguments": {"policy_ref": policy_ref}}]}
        pol = next((o for o in outs if "product" in o), {})
        hist = next((o for o in outs if "claims" in o), {})
        n = hist.get("claims_last_12_months", 0)
        return {"content": {
            "policy_found": bool(pol),
            "ped_link_suspected": False,
            "ped_link_reason": "",
            "claim_history_note": f"{n} claim(s) in the last 12 months." if n else "No claims in the last 12 months.",
            "coverage_notes": f"{pol.get('product', 'Policy')} retrieved; waiting periods, room-rent cap, sub-limits and co-pay passed to the rule engine.",
        }}
    return fn


def policy_agent(ctx: StepContext) -> dict:
    """Tools: get_policy, get_claim_history -> rules.py -> eligible amount + clause-cited deductions."""
    ex, form = ctx.state["intake"]["extraction"], ctx.request["form"]
    resolve, adm = _resolver(ctx), _admission(ctx)
    ref = ex.get("policy_ref") or "[POLICY_1]"
    allow = [
        Tool("get_policy", "Fetch policy terms (waiting periods, room-rent cap, sub-limits, co-pay, exclusions) by policy token.",
             POLICY_REF, lambda policy_ref: tools.get_policy(policy_ref, resolve)),
        Tool("get_claim_history", "Fetch prior claims on the policy by policy token.",
             POLICY_REF, lambda policy_ref: tools.get_claim_history(policy_ref, resolve, adm)),
    ]
    user = (f"Policy token: {ref}. Admission date: {ex['admission_date']}. Diagnosis: {ex['provisional_diagnosis']} "
            f"(ICD-10 {', '.join(ex['icd10_codes']) or 'not stated'}). Proposed procedure: {ex['proposed_procedure']}.")
    result = ctx.llm.run_agent(agent="policy", model=ctx.settings.model_fast, system=prompt("policy"), user=user,
                               schema=PolicyFindings, mock=mock_policy(ref), tools=allow, on_tool=_tool_hook(ctx, "policy"),
                               force_mock=ctx.force_mock)
    findings: PolicyFindings = result.data
    ctx.emit("policy", "coverage_review", input_summary="policy terms + claim history", output=findings.model_dump(),
             model=ctx.model_label(ctx.settings.model_fast, result.source), tokens=result.tokens, latency_ms=result.latency_ms)

    t0 = time.perf_counter()
    policy, procedure = tools.policy_by_id(form["policy_number"]), resolve_procedure(form)
    if not policy or not procedure:
        raise ValueError("policy or procedure package not found in the reference data")
    pdoc = tools.policies_doc()
    ev = rules.evaluate(policy, procedure, claim_input(form), pdoc["ped_icd10_prefixes"], pdoc["clause_refs_default"])
    history = tools.get_claim_history(ref, resolve, adm)
    ctx.emit("policy", "rules_engine", tool="rules.py", latency_ms=_ms(t0), status="alert" if ev.failed_rules else "ok",
             input_summary=f"{len(ev.rules)} rules on {inr(ev.claimed_amount)} requested",
             output={"eligible_amount": inr(ev.eligible_amount), "failed_rules": ev.failed_rules,
                     "deductions": [f"{d['label']} {inr(d['amount'])}" for d in ev.deductions]})
    return {"policy": {
        "findings": findings.model_dump(),
        "product": policy["product"],
        "procedure_key": procedure["key"],
        "procedure_name": procedure["name"],
        "policy_age_months": months_between(date.fromisoformat(policy["inception_date"]), adm),
        "claims_last_12_months": history.get("claims_last_12_months", 0),
        "evaluation": ev.model_dump(mode="json"),
        "llm_source": result.source,
    }}


# ======================= Docs & Coding Agent =======================

class DocsFindings(BaseModel):
    procedure_key: str
    coding_consistent: bool
    coding_note: str = ""
    missing_docs: list[str] = []
    documentation_note: str = ""


PROC_KEY = {"type": "object", "properties": {"procedure_key": {"type": "string"}}, "required": ["procedure_key"]}
LOOKUP = {"type": "object", "properties": {"query": {"type": "string"}, "icd10": {"type": "string"}}, "required": ["query"]}


def mock_docs(ex: dict, attached: list[str]):
    query, code = f"{ex['proposed_procedure']} {ex['provisional_diagnosis']}", (ex.get("icd10_codes") or [""])[0]

    def fn(messages: list[dict]) -> dict:
        outs = _tool_outputs(messages)
        lookup = next((o for o in outs if "candidates" in o), None)
        if lookup is None:
            return {"tool_calls": [{"name": "lookup_procedure", "arguments": {"query": query, "icd10": code}}]}
        cands = lookup["candidates"]
        key = cands[0]["procedure_key"] if cands else ""
        req = next((o for o in outs if "required_docs" in o), None)
        if req is None and key:
            return {"tool_calls": [{"name": "required_docs", "arguments": {"procedure_key": key}},
                                   {"name": "benchmark_rate", "arguments": {"procedure_key": key}}]}
        missing = [d["code"] for d in (req or {}).get("required_docs", []) if d["code"] not in attached]
        consistent = bool(cands) and code in cands[0]["icd10"]
        return {"content": {
            "procedure_key": key,
            "coding_consistent": consistent,
            "coding_note": f"ICD-10 {code} fits the {cands[0]['name']} package." if consistent else f"ICD-10 {code} does not fit the selected package.",
            "missing_docs": missing,
            "documentation_note": ("Please share: " + "; ".join(tools.doc_label(d) for d in missing) + ".") if missing else "",
        }}
    return fn


def docs_agent(ctx: StepContext) -> dict:
    """Tools: lookup_procedure, required_docs, benchmark_rate -> missing documents, cost deviation %."""
    ex, form = ctx.state["intake"]["extraction"], ctx.request["form"]
    attached = list(ctx.request.get("attached_docs", []))
    allow = [
        Tool("lookup_procedure", "Search the procedure package table by free text and an ICD-10 code; returns up to 3 candidates.",
             LOOKUP, tools.lookup_procedure),
        Tool("required_docs", "List the documents required for a package.", PROC_KEY, tools.required_docs),
        Tool("benchmark_rate", "Benchmark package rate (INR) and typical length of stay for a package.", PROC_KEY, tools.benchmark_rate),
    ]
    user = (f"Proposed procedure: {ex['proposed_procedure']}. Diagnosis: {ex['provisional_diagnosis']}. "
            f"ICD-10 codes: {', '.join(ex['icd10_codes']) or 'none'}. Attached document codes: {', '.join(attached)}.")
    result = ctx.llm.run_agent(agent="docs", model=ctx.settings.model_fast, system=prompt("docs"), user=user,
                               schema=DocsFindings, mock=mock_docs(ex, attached), tools=allow, on_tool=_tool_hook(ctx, "docs"),
                               force_mock=ctx.force_mock)
    f: DocsFindings = result.data
    ctx.emit("docs", "coding_review", input_summary="package table lookups", output=f.model_dump(),
             model=ctx.model_label(ctx.settings.model_fast, result.source), tokens=result.tokens, latency_ms=result.latency_ms)

    t0 = time.perf_counter()
    rules_key = ctx.state.get("policy", {}).get("procedure_key")
    proc = tools.procedure_by_key(f.procedure_key) or tools.procedure_by_key(rules_key or "") or resolve_procedure(form)
    missing = [d for d in proc["required_docs"] if d not in attached]
    coding_ok = f.coding_consistent and form["icd10"].upper() in proc["icd10"] and rules_key in (None, proc["key"])
    claimed, bench = sum(form["estimate"].values()), proc["benchmark_rate_inr"]
    deviation = round((claimed - bench) / bench * 100, 1)
    los_gap = form["expected_los_days"] - proc["typical_los_days"]
    ctx.emit("docs", "documentation_check", tool="required_docs", latency_ms=_ms(t0), status="alert" if missing else "ok",
             input_summary=f"{len(attached)} attached vs {len(proc['required_docs'])} required",
             output={"missing": [tools.doc_label(d) for d in missing]})
    ctx.emit("docs", "cost_benchmark", tool="benchmark_rate", status="alert" if deviation > ctx.config["risk"]["cost_deviation_flag_pct"] else "ok",
             output={"requested": inr(claimed), "benchmark": inr(bench), "deviation_pct": deviation, "los_gap_days": los_gap})
    return {"docs": {
        "procedure_key": proc["key"],
        "procedure_name": proc["name"],
        "coding_consistent": coding_ok,
        "coding_note": f.coding_note,
        "missing_docs": missing,
        "missing_labels": [tools.doc_label(d) for d in missing],
        "documentation_note": f.documentation_note,
        "required_docs": [{"code": d, "label": tools.doc_label(d), "attached": d in attached} for d in proc["required_docs"]],
        "benchmark_rate_inr": bench,
        "cost_deviation_pct": deviation,
        "typical_los_days": proc["typical_los_days"],
        "los_gap_days": los_gap,
        "llm_source": result.source,
    }}


# ======================= Risk Agent =======================

class RiskRationale(BaseModel):
    rationale: str
    top_concerns: list[str] = []


def risk_signals(ctx: StepContext) -> list[dict]:
    """Deterministic risk signals; weights live in pack.yaml."""
    cfg, form, st = ctx.config, ctx.request["form"], ctx.state
    weights, flag = cfg["risk"]["weights"], cfg["risk"]["cost_deviation_flag_pct"]
    intake, policy, docs = st.get("intake", {}), st.get("policy", {}), st.get("docs", {})
    ev = policy.get("evaluation", {})
    out: list[dict] = []

    def add(sig: str, label: str, detail: str) -> None:
        out.append({"id": sig, "label": label, "weight": int(weights.get(sig, 0)), "detail": detail})

    if intake.get("guard", {}).get("detected"):
        add("injection_detected", "Prompt-injection attempt in the note", "Instruction-like text was found and quarantined before any model read it.")
    if ev.get("failed_rules"):
        add("eligibility_rule_failed", "Eligibility rule failed", ", ".join(ev["failed_rules"]))
    dev, claimed, bench = docs.get("cost_deviation_pct", 0), ev.get("claimed_amount", 0), docs.get("benchmark_rate_inr", 0)
    if dev > flag:
        add("cost_deviation_high", f"Estimate {dev:.0f}% above benchmark", f"{inr(claimed)} requested vs package benchmark {inr(bench)}")
    elif dev > 10:
        add("cost_deviation_moderate", f"Estimate {dev:.0f}% above benchmark", f"{inr(claimed)} requested vs package benchmark {inr(bench)}")
    if intake and intake.get("confidence", 1) < cfg["decision_policy"]["min_extraction_confidence"]:
        add("low_confidence", "Low extraction confidence", f"Confidence {intake['confidence']:.2f}: {'; '.join(intake.get('cross_check_issues', []))[:160]}")
    age = policy.get("policy_age_months")
    if age is not None and age < 12:
        add("new_policy", f"Policy only {age} months old", "Claims in the first year carry higher non-disclosure risk.")
    duration = intake.get("extraction", {}).get("symptom_duration_months")
    if duration and age is not None and duration > age:
        add("symptoms_predate_policy", "Symptoms predate policy inception", f"Note states about {duration} months of symptoms; the policy is {age} months old.")
    if policy.get("claims_last_12_months", 0) >= 3:
        add("claim_frequency", "Frequent recent claims", f"{policy['claims_last_12_months']} claims in the last 12 months.")
    if docs.get("los_gap_days", 0) > 1:
        add("los_above_typical", "Stay longer than typical", f"{form['expected_los_days']} days requested vs {docs['typical_los_days']} typical.")
    if ev.get("amount_before_blocking", 0) > cfg["decision_policy"]["auto_threshold_inr"]:
        add("amount_over_threshold", "High-value request", f"Above the {inr(cfg['decision_policy']['auto_threshold_inr'])} desk threshold.")
    if any(r["rule_id"] == "room_rent_cap" and not r["passed"] for r in ev.get("rules", [])):
        add("room_rent_over_cap", "Room rent above cap", "Proportionate deduction may apply at the final bill.")
    if docs and not docs.get("coding_consistent", True):
        add("coding_inconsistent", "Coding inconsistency", docs.get("coding_note", "ICD-10 does not fit the package."))
    if docs.get("missing_docs"):
        add("missing_documents", "Documents missing", ", ".join(docs.get("missing_labels", [])))
    if policy.get("findings", {}).get("ped_link_suspected"):
        add("ped_link_flagged", "Possible link to a declared PED", policy["findings"].get("ped_link_reason", ""))
    return out


def risk_agent(ctx: StepContext) -> dict:
    """Rule signals + LLM rationale -> risk score 0-100 with reasons."""
    t0 = time.perf_counter()
    signals = risk_signals(ctx)
    score = min(100, sum(s["weight"] for s in signals))
    level = "high" if score >= ctx.config["decision_policy"]["risk_escalate_at"] else "medium" if score >= 30 else "low"
    ctx.emit("risk", "rule_signals", tool="risk_rules", latency_ms=_ms(t0), status="alert" if level == "high" else "ok",
             output={"score": score, "level": level, "signals": [f"{s['label']} (+{s['weight']})" for s in signals]})
    top = sorted(signals, key=lambda s: -s["weight"])[:3]
    mock_text = (f"Risk score {score}/100 ({level}). Main drivers: " + "; ".join(s["label"] for s in top) + "."
                 if signals else f"Risk score {score}/100 (low). No risk signals fired.")
    result = ctx.llm.run_agent(
        agent="risk", model=ctx.settings.model_reason, system=prompt("risk"),
        user=json.dumps({"score": score, "level": level, "signals": [{"label": s["label"], "detail": s["detail"]} for s in signals]}, ensure_ascii=False),
        schema=RiskRationale, mock=lambda _m: {"content": {"rationale": mock_text, "top_concerns": [s["label"] for s in top]}},
        force_mock=ctx.force_mock,
    )
    ctx.emit("risk", "rationale", model=ctx.model_label(ctx.settings.model_reason, result.source), tokens=result.tokens,
             latency_ms=result.latency_ms, output=result.data.model_dump())
    return {"risk": {"score": score, "level": level, "signals": signals, "rationale": result.data.rationale,
                     "top_concerns": result.data.top_concerns, "llm_source": result.source}}


# ======================= Orchestrator decision policy =======================

def decide_step(ctx: StepContext) -> dict:
    """Fixed decision policy from pack.yaml. The AI can recommend, query or escalate; it can never deny."""
    cfg, st = ctx.config["decision_policy"], ctx.state
    intake, policy, docs, risk = st.get("intake", {}), st.get("policy", {}), st.get("docs", {}), st.get("risk", {})
    ev = policy.get("evaluation", {})
    labels = {r["rule_id"]: (r["label"], r["clause_ref"]) for r in ev.get("rules", [])}
    reasons: list[str] = []
    if st.get("step_failure"):
        f = st["step_failure"]
        reasons.append(f"Automated step '{f['step']}' failed ({f['reason'][:120]}); routed to a human")
    if intake.get("guard", {}).get("detected"):
        reasons.append("Prompt-injection attempt detected in the admission note")
    for rid in ev.get("failed_rules", []):
        label, clause = labels.get(rid, (rid, ""))
        reasons.append(f"Rule failed: {label} ({clause})")
    if intake and intake.get("confidence", 1) < cfg["min_extraction_confidence"]:
        reasons.append(f"Extraction confidence {intake['confidence']:.2f} below {cfg['min_extraction_confidence']}")
    if risk and risk.get("score", 0) >= cfg["risk_escalate_at"]:
        reasons.append(f"Risk score {risk['score']} at or above {cfg['risk_escalate_at']}")
    amount = ev.get("amount_before_blocking", 0)
    if amount > cfg["auto_threshold_inr"]:
        reasons.append(f"Amount {inr(amount)} above the {inr(cfg['auto_threshold_inr'])} desk threshold")
    missing = docs.get("missing_docs", [])
    outcome = "ESCALATE_MEDICAL_OFFICER" if reasons else "QUERY_HOSPITAL" if missing else "RECOMMEND_APPROVE"
    level = ctx.runtime.autonomy_level if ctx.runtime else cfg["autonomy_level"]
    decision = {
        "outcome": outcome,
        "reasons": reasons,
        "recommended_amount": int(ev.get("eligible_amount", 0)),
        "claimed_amount": int(ev.get("claimed_amount", 0)),
        "amount_before_blocking": int(amount),
        "missing_docs": missing,
        "autonomy_level": level,
        "one_click": outcome == "RECOMMEND_APPROVE" and level == 2,
        "threshold_inr": cfg["auto_threshold_inr"],
    }
    if ctx.runtime:
        ctx.runtime.cases.update(ctx.case_id, recommendation=outcome, recommended_amount=decision["recommended_amount"],
                                 risk_score=risk.get("score"))
    ctx.emit("orchestrator", "decision_policy", tool="pack.yaml", input_summary=f"autonomy level {level}",
             output={"outcome": outcome, "reasons": reasons, "recommended": inr(decision["recommended_amount"])},
             status="alert" if outcome == "ESCALATE_MEDICAL_OFFICER" else "ok")
    return {"decision": decision}


class DecisionSummary(BaseModel):
    summary: str
    cited_rules: list[str] = []


def template_summary(decision: dict, ev: dict, docs: dict) -> tuple[str, list[str]]:
    deductions = ev.get("deductions", [])
    if decision["outcome"] == "RECOMMEND_APPROVE":
        text = f"Recommend approving {inr(decision['recommended_amount'])} of the {inr(decision['claimed_amount'])} requested. "
        text += ("Deductions: " + "; ".join(f"{d['label']} {inr(d['amount'])} ({d['clause_ref']})" for d in deductions) + ". "
                 if deductions else "No deductions apply. ")
        return text + "All eligibility rules passed.", [d["rule_id"] for d in deductions]
    if decision["outcome"] == "QUERY_HOSPITAL":
        return (f"Required documents are missing: {', '.join(docs.get('missing_labels', []))}. A query has gone to the hospital "
                "and the SLA clock is paused until they arrive.", [])
    failed = ev.get("failed_rules", [])
    return ("Escalated to the Medical Officer: " + "; ".join(decision["reasons"][:3]) + ". The AI cannot deny; a Medical Officer "
            "must review the evidence and record a reason.", failed)


def explain_step(ctx: StepContext) -> dict:
    """LLM summary that may only reference the supplied rule results; unsupported figures are rejected."""
    decision, ev, docs = ctx.state["decision"], ctx.state.get("policy", {}).get("evaluation", {}), ctx.state.get("docs", {})
    payload = {
        "outcome": decision["outcome"], "reasons": decision["reasons"],
        "requested": inr(decision["claimed_amount"]), "recommended": inr(decision["recommended_amount"]),
        "rules": [{"rule_id": r["rule_id"], "label": r["label"], "passed": r["passed"], "impact": inr(r["impact_inr"]),
                   "clause_ref": r["clause_ref"], "explanation": r["explanation"]} for r in ev.get("rules", [])],
        "risk": {"score": ctx.state.get("risk", {}).get("score"), "signals": [s["label"] for s in ctx.state.get("risk", {}).get("signals", [])]},
        "missing_docs": docs.get("missing_labels", []),
    }
    text = json.dumps(payload, ensure_ascii=False)
    fallback, fallback_cited = template_summary(decision, ev, docs)
    result = ctx.llm.run_agent(agent="explain", model=ctx.settings.model_reason, system=prompt("decision"), user=text,
                               schema=DecisionSummary, mock=lambda _m: {"content": {"summary": fallback, "cited_rules": fallback_cited}},
                               force_mock=ctx.force_mock)
    allowed_rules = {r["rule_id"] for r in ev.get("rules", [])}
    unsupported = sorted(rupee_figures(result.data.summary) - rupee_figures(text) - rupee_figures(fallback))
    grounded = not unsupported
    summary = result.data.summary if grounded else fallback
    cited = [r for r in result.data.cited_rules if r in allowed_rules] if grounded else fallback_cited
    ctx.emit("orchestrator", "explain_decision", model=ctx.model_label(ctx.settings.model_reason, result.source),
             tokens=result.tokens, latency_ms=result.latency_ms, status="ok" if grounded else "alert",
             output={"summary": summary, "cited_rules": cited, "grounded": grounded, "rejected_figures": unsupported})
    return {"explanation": {"summary": summary, "cited_rules": cited, "grounded": grounded,
                            "rejected_figures": unsupported, "llm_source": result.source}}


def act_step(ctx: StepContext) -> dict:
    """Route the decision: autonomous hospital query, escalation, one-click sign-off, or (level 3) auto-approve."""
    rt, decision = ctx.runtime, ctx.state["decision"]
    params = {"force_mock": ctx.force_mock}
    if decision["outcome"] == "QUERY_HOSPITAL":
        rt.gateway.execute("query_hospital", case_id=ctx.case_id, role="SYSTEM", actor="agent:action",
                           params={**params, "missing": decision["missing_docs"]})
        return {"_pause": "QUERY_RAISED", "_resume_at": "docs"}
    if decision["outcome"] == "ESCALATE_MEDICAL_OFFICER":
        rt.gateway.execute("escalate", case_id=ctx.case_id, role="SYSTEM", actor="agent:orchestrator",
                           params={**params, "reasons": decision["reasons"]})
        return {}
    level = rt.autonomy_level
    if level >= 3 and decision["recommended_amount"] <= decision["threshold_inr"]:
        rt.gateway.execute("auto_approve", case_id=ctx.case_id, role="SYSTEM", actor="agent:action",
                           params={**params, "amount": decision["recommended_amount"]})
        return {}
    rt.cases.update(ctx.case_id, status="AWAITING_APPROVAL", assigned_role="PROCESSOR")
    ctx.emit("action", "route_to_processor", tool="action_gateway",
             output={"queue": "Processor", "one_click": level == 2, "prefilled_amount": inr(decision["recommended_amount"]) if level == 2 else None})
    return {}
