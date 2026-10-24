"""Health pre-authorization agents.

Each agent is a function with its own system prompt (prompts/*.md) and an allow-list of tools.
Mock fixtures sit next to each agent: deterministic functions of the agent's own inputs, used in
mock mode, as the replay-miss fallback, and by the test suite.
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from core.guard import screen
from core.orchestrator import StepContext
from core.redaction import Redactor
from packs.health_preauth import tools
from packs.health_preauth.rules import inr

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
