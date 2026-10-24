"""Deterministic coverage rules for cashless pre-authorization.

Pure functions, unit-tested. The LLM never does this money math or decides eligibility.
Each rule returns {rule_id, clause_ref, passed, impact_inr, explanation}; impact is negative for a deduction.
Blocking rules (policy period, waiting periods, exclusions) make the case ineligible for AI approval.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, computed_field

DISEASE_LABELS = {
    "cataract": "Cataract surgery",
    "joint_replacement": "Joint replacement",
    "hernia": "Hernia repair",
    "gall_bladder": "Gall bladder surgery",
    "prostate": "Prostate surgery",
    "hysterectomy": "Hysterectomy",
    "kidney_stone": "Kidney stone treatment",
    "tonsils": "Tonsil surgery",
    "sinus_septum": "Nasal septum surgery",
}
EXCLUSION_LABELS = {
    "obesity_surgery": "Bariatric / obesity surgery",
    "cosmetic_surgery": "Cosmetic surgery",
    "infertility": "Infertility treatment",
    "dental": "Dental treatment",
    "experimental": "Experimental treatment",
    "maternity": "Maternity",
}


class RuleResult(BaseModel):
    rule_id: str
    label: str
    clause_ref: str
    passed: bool
    blocking: bool
    impact_inr: int = 0
    explanation: str


class ClaimInput(BaseModel):
    admission_date: date
    is_accident: bool = False
    icd10_codes: list[str]
    room_rent_per_day: int
    los_days: int
    estimate: dict[str, int]

    @computed_field
    @property
    def total(self) -> int:
        return int(sum(self.estimate.values()))


class Evaluation(BaseModel):
    claimed_amount: int
    eligible_amount: int
    amount_before_blocking: int
    deductions: list[dict]
    failed_rules: list[str]
    rules: list[RuleResult]
    proportionate_deduction_risk: bool


def inr(amount: int | float) -> str:
    """Indian digit grouping: 110000 -> ₹1,10,000."""
    value = int(round(amount))
    digits = str(abs(value))
    if len(digits) > 3:
        head, tail, groups = digits[:-3], digits[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return ("-" if value < 0 else "") + "₹" + digits


def months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    return months - (1 if end.day < start.day else 0)


def _clause(policy: dict, rule_id: str, defaults: dict) -> str:
    return policy.get("clause_refs", {}).get(rule_id) or defaults.get(rule_id, rule_id)


def _d(value: str) -> date:
    return date.fromisoformat(value)


def rule_policy_active(policy: dict, admission: date, clauses: dict) -> RuleResult:
    start, end = _d(policy["inception_date"]), _d(policy["expiry_date"])
    ok = policy.get("status") == "active" and start <= admission <= end
    why = (f"Policy in force {start:%d %b %Y} to {end:%d %b %Y}; admission on {admission:%d %b %Y}."
           if ok else f"Admission on {admission:%d %b %Y} is outside the policy period or the policy is not active.")
    return RuleResult(rule_id="policy_active", label="Policy active on admission date", clause_ref=_clause(policy, "policy_active", clauses),
                      passed=ok, blocking=True, explanation=why)


def rule_initial_waiting(policy: dict, admission: date, is_accident: bool, clauses: dict) -> RuleResult:
    required = policy["waiting_periods"]["initial_days"]
    days = (admission - _d(policy["inception_date"])).days
    if is_accident:
        ok, why = True, f"Accident-related admission: the {required}-day initial waiting period does not apply."
    else:
        ok = days >= required
        why = f"Admission is {days} days after policy inception; the initial waiting period is {required} days."
    return RuleResult(rule_id="initial_waiting", label="Initial waiting period", clause_ref=_clause(policy, "initial_waiting", clauses),
                      passed=ok, blocking=True, explanation=why)


def rule_ped_waiting(policy: dict, icd10_codes: list[str], admission: date, ped_prefixes: dict, clauses: dict) -> RuleResult:
    codes = [c.upper().strip() for c in icd10_codes]
    related = [p for p in policy.get("ped_declared", []) if any(c.startswith(pre) for c in codes for pre in ped_prefixes.get(p, []))]
    required = policy["waiting_periods"]["ped_months"]
    months = months_between(_d(policy["inception_date"]), admission)
    declared = ", ".join(p.replace("_", " ") for p in policy.get("ped_declared", [])) or "none"
    if not related:
        ok, why = True, f"Diagnosis is not linked to a declared pre-existing disease (declared: {declared})."
    else:
        ok = months >= required
        why = f"Diagnosis relates to declared PED ({', '.join(related)}); policy is {months} months old against a {required}-month PED waiting period."
    return RuleResult(rule_id="ped_waiting", label="Pre-existing disease waiting period", clause_ref=_clause(policy, "ped_waiting", clauses),
                      passed=ok, blocking=True, explanation=why)


def rule_specific_disease_waiting(policy: dict, procedure: dict, admission: date, clauses: dict) -> RuleResult:
    key = procedure.get("specific_disease")
    required = policy["waiting_periods"].get("specific_disease", {}).get(key) if key else None
    months = months_between(_d(policy["inception_date"]), admission)
    if required is None:
        ok, why = True, f"No specified-disease waiting period applies to {procedure['name'].lower()}."
    else:
        ok = months >= required
        label = DISEASE_LABELS.get(key, key.replace("_", " ").capitalize())
        why = f"{label} has a {required}-month waiting period; the policy is {months} months old on the admission date."
    return RuleResult(rule_id="specific_disease_waiting", label="Specified-disease waiting period",
                      clause_ref=_clause(policy, "specific_disease_waiting", clauses), passed=ok, blocking=True, explanation=why)


def rule_exclusions(policy: dict, procedure: dict, clauses: dict) -> RuleResult:
    category = procedure.get("category", "")
    ok = category not in policy.get("exclusions", [])
    why = ("Procedure is not in the policy's permanent exclusions." if ok
           else f"{EXCLUSION_LABELS.get(category, category)} is a permanent exclusion under this policy.")
    return RuleResult(rule_id="exclusions", label="Permanent exclusions", clause_ref=_clause(policy, "exclusions", clauses),
                      passed=ok, blocking=True, explanation=why)


def rule_room_rent_cap(policy: dict, room_rent_per_day: int, los_days: int, clauses: dict) -> RuleResult:
    cap = policy["room_rent_cap_per_day"]
    over = max(0, room_rent_per_day - cap)
    excess = over * los_days
    if excess == 0:
        why = f"Room rent {inr(room_rent_per_day)}/day is within the cap of {inr(cap)}/day."
    else:
        why = (f"Room rent {inr(room_rent_per_day)}/day exceeds the cap of {inr(cap)}/day: {inr(over)} x {los_days} days = "
               f"{inr(excess)} deducted. Proportionate deduction on associated charges may apply at the final bill.")
    return RuleResult(rule_id="room_rent_cap", label="Room-rent cap", clause_ref=_clause(policy, "room_rent_cap", clauses),
                      passed=excess == 0, blocking=False, impact_inr=-excess, explanation=why)


def rule_sub_limit(policy: dict, procedure: dict, amount: int, clauses: dict) -> RuleResult:
    key = procedure.get("sub_limit_key")
    limit = policy.get("sub_limits", {}).get(key) if key else None
    if limit is None:
        ok, impact, why = True, 0, f"No procedure sub-limit applies to {procedure['name'].lower()}."
    elif amount > limit:
        ok, impact = False, -(amount - limit)
        why = f"{procedure['name']} is sub-limited to {inr(limit)}; admissible {inr(amount)} is reduced by {inr(amount - limit)}."
    else:
        ok, impact, why = True, 0, f"Admissible {inr(amount)} is within the {inr(limit)} sub-limit."
    return RuleResult(rule_id="sub_limit", label="Procedure sub-limit", clause_ref=_clause(policy, "sub_limit", clauses),
                      passed=ok, blocking=False, impact_inr=impact, explanation=why)


def rule_copay(policy: dict, amount: int, clauses: dict) -> RuleResult:
    pct = policy.get("copay_pct", 0)
    impact = -int(round(amount * pct / 100))
    why = f"No co-payment on this policy." if pct == 0 else f"{pct}% co-payment on {inr(amount)} = {inr(-impact)} borne by the insured."
    return RuleResult(rule_id="copay", label="Co-payment", clause_ref=_clause(policy, "copay", clauses),
                      passed=True, blocking=False, impact_inr=impact, explanation=why)


def rule_balance_cap(policy: dict, amount: int, clauses: dict) -> RuleResult:
    balance = policy["balance"]
    if amount > balance:
        ok, impact = False, -(amount - balance)
        why = f"Payable {inr(amount)} exceeds the available sum insured of {inr(balance)}; capped at the balance."
    else:
        ok, impact, why = True, 0, f"Within the available sum insured of {inr(balance)}."
    return RuleResult(rule_id="balance_cap", label="Cap at available balance", clause_ref=_clause(policy, "balance_cap", clauses),
                      passed=ok, blocking=False, impact_inr=impact, explanation=why)


def evaluate(policy: dict, procedure: dict, claim: ClaimInput, ped_prefixes: dict, clauses: dict) -> Evaluation:
    """Run every rule in order. Money rules chain: room rent -> sub-limit -> co-pay -> balance."""
    adm = claim.admission_date
    eligibility = [
        rule_policy_active(policy, adm, clauses),
        rule_initial_waiting(policy, adm, claim.is_accident, clauses),
        rule_ped_waiting(policy, claim.icd10_codes, adm, ped_prefixes, clauses),
        rule_specific_disease_waiting(policy, procedure, adm, clauses),
        rule_exclusions(policy, procedure, clauses),
    ]
    amount = claim.total
    room = rule_room_rent_cap(policy, claim.room_rent_per_day, claim.los_days, clauses)
    amount += room.impact_inr
    sub = rule_sub_limit(policy, procedure, amount, clauses)
    amount += sub.impact_inr
    copay = rule_copay(policy, amount, clauses)
    amount += copay.impact_inr
    balance = rule_balance_cap(policy, amount, clauses)
    amount += balance.impact_inr
    money = [room, sub, copay, balance]
    failed = [r.rule_id for r in eligibility if not r.passed]
    return Evaluation(
        claimed_amount=claim.total,
        eligible_amount=0 if failed else amount,
        amount_before_blocking=amount,
        deductions=[{"rule_id": r.rule_id, "label": r.label, "amount": -r.impact_inr, "clause_ref": r.clause_ref} for r in money if r.impact_inr < 0],
        failed_rules=failed,
        rules=eligibility + money,
        proportionate_deduction_risk=room.impact_inr < 0,
    )
