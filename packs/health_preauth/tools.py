"""Pack tools: plain Python functions over the synthetic data, exposed to agents via tool calling.

Tools receive tokens (e.g. [POLICY_1]) from the model and resolve them locally, so the model never
sees a real policy number. Tool outputs carry no personal data.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

DATA = Path(__file__).resolve().parent / "data"
CASES = DATA / "cases"

Resolver = Callable[[str], str]


@lru_cache(maxsize=1)
def policies_doc() -> dict:
    return json.loads((DATA / "policies.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def procedures_doc() -> dict:
    return json.loads((DATA / "procedures.json").read_text(encoding="utf-8"))


def policy_by_id(policy_id: str) -> dict | None:
    return next((p for p in policies_doc()["policies"] if p["policy_id"] == policy_id), None)


def procedure_by_key(key: str) -> dict | None:
    return next((p for p in procedures_doc()["procedures"] if p["key"] == key), None)


def doc_label(code: str) -> str:
    return procedures_doc()["doc_labels"].get(code, code.replace("_", " ").capitalize())


def doc_codes() -> list[str]:
    return list(procedures_doc()["doc_labels"].keys())


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("PA-*.json"))]


def load_case(case_id: str) -> dict | None:
    path = CASES / f"{case_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def note_text(request: dict) -> str:
    """Admission note as text; PDFs are read with pypdf (hidden text included, as an LLM would see it)."""
    if request.get("admission_note_pdf"):
        reader = PdfReader(str(CASES / request["admission_note_pdf"]))
        return "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    return request.get("admission_note", "")


# ---------------- tools exposed to agents ----------------

def _clean_ref(ref: str) -> str:
    ref = (ref or "").strip()
    return ref if ref.startswith("[") else f"[{ref}]"


def get_policy(policy_ref: str, resolve: Resolver) -> dict:
    policy = policy_by_id(resolve(_clean_ref(policy_ref)))
    if not policy:
        return {"error": "policy not found"}
    keys = ("product", "status", "sum_insured", "balance", "inception_date", "expiry_date", "ped_declared",
            "waiting_periods", "room_rent_cap_per_day", "copay_pct", "sub_limits", "exclusions")
    return {"policy_ref": _clean_ref(policy_ref), **{k: policy[k] for k in keys}}


def get_claim_history(policy_ref: str, resolve: Resolver, as_of: date) -> dict:
    policy = policy_by_id(resolve(_clean_ref(policy_ref)))
    if not policy:
        return {"error": "policy not found"}
    claims = policy.get("claim_history", [])
    recent = [c for c in claims if date.fromisoformat(c["date"]) >= as_of - timedelta(days=365)]
    return {"policy_ref": _clean_ref(policy_ref), "claims": claims, "claims_last_12_months": len(recent),
            "amount_last_12_months": sum(c["amount"] for c in recent)}


def lookup_procedure(query: str, icd10: str = "") -> dict:
    q, code = (query or "").lower(), (icd10 or "").upper().strip()
    scored = []
    for p in procedures_doc()["procedures"]:
        score = 0
        if code and code in p["icd10"]:
            score += 3
        elif code and any(c[:3] == code[:3] for c in p["icd10"]):
            score += 2
        score += sum(1 for kw in p["keywords"] if kw in q)
        if score:
            scored.append((score, p))
    scored.sort(key=lambda sp: -sp[0])
    return {"candidates": [
        {"procedure_key": p["key"], "name": p["name"], "icd10": p["icd10"], "category": p["category"],
         "benchmark_rate_inr": p["benchmark_rate_inr"], "typical_los_days": p["typical_los_days"], "match_score": s}
        for s, p in scored[:3]
    ]}


def required_docs(procedure_key: str) -> dict:
    p = procedure_by_key(procedure_key)
    if not p:
        return {"error": f"unknown procedure_key {procedure_key}"}
    return {"procedure_key": procedure_key, "required_docs": [{"code": d, "label": doc_label(d)} for d in p["required_docs"]]}


def benchmark_rate(procedure_key: str) -> dict:
    p = procedure_by_key(procedure_key)
    if not p:
        return {"error": f"unknown procedure_key {procedure_key}"}
    return {"procedure_key": procedure_key, "benchmark_rate_inr": p["benchmark_rate_inr"], "typical_los_days": p["typical_los_days"]}
