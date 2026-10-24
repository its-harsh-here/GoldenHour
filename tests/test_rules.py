from datetime import date

from packs.health_preauth import rules, tools
from packs.health_preauth.rules import ClaimInput, evaluate, inr, months_between

PED = tools.policies_doc()["ped_icd10_prefixes"]
CLAUSES = tools.policies_doc()["clause_refs_default"]


def claim_for(case_id: str) -> ClaimInput:
    form = tools.load_case(case_id)["form"]
    return ClaimInput(admission_date=date.fromisoformat(form["admission_date"]), is_accident=form["is_accident"],
                      icd10_codes=[form["icd10"]], room_rent_per_day=form["room_rent_per_day"],
                      los_days=form["expected_los_days"], estimate=form["estimate"])


def run(case_id: str, procedure_key: str):
    form = tools.load_case(case_id)["form"]
    return evaluate(tools.policy_by_id(form["policy_number"]), tools.procedure_by_key(procedure_key), claim_for(case_id), PED, CLAUSES)


def test_inr_uses_indian_grouping():
    assert inr(110000) == "₹1,10,000"
    assert inr(1000) == "₹1,000"
    assert inr(999) == "₹999"
    assert inr(10000000) == "₹1,00,00,000"
    assert inr(-9000) == "-₹9,000"


def test_months_between():
    assert months_between(date(2026, 2, 20), date(2026, 10, 24)) == 8
    assert months_between(date(2023, 9, 12), date(2026, 10, 24)) == 37
    assert months_between(date(2026, 1, 31), date(2026, 2, 28)) == 0


def test_hero_a_room_rent_deduction_is_cited():
    ev = run("PA-1001", "lap_appendectomy")
    assert ev.claimed_amount == 110000
    assert ev.eligible_amount == 101000
    assert ev.failed_rules == []
    assert ev.deductions == [{"rule_id": "room_rent_cap", "label": "Room-rent cap", "amount": 9000, "clause_ref": "Sec 4.2 Room Rent"}]
    assert ev.proportionate_deduction_risk


def test_hero_b_sub_limit_then_copay():
    ev = run("PA-1002", "cataract_phaco")
    assert ev.claimed_amount == 52000
    assert ev.eligible_amount == 36000
    assert [(d["rule_id"], d["amount"]) for d in ev.deductions] == [("sub_limit", 12000), ("copay", 4000)]


def test_hero_c_specific_disease_waiting_blocks():
    ev = run("PA-1003", "tkr")
    assert ev.failed_rules == ["specific_disease_waiting"]
    assert ev.eligible_amount == 0
    assert ev.amount_before_blocking == 250000
    failed = next(r for r in ev.rules if r.rule_id == "specific_disease_waiting")
    assert "24-month" in failed.explanation and "8 months" in failed.explanation


def test_initial_waiting_fails_unless_accident():
    assert run("PA-1006", "pneumonia_medical").failed_rules == ["initial_waiting"]
    accident = run("PA-1008", "orif_tibia")
    assert accident.failed_rules == [] and accident.eligible_amount == 135000


def test_exclusion_blocks_bariatric_surgery():
    assert run("PA-1009", "sleeve_gastrectomy").failed_rules == ["exclusions"]


def test_balance_cap_after_copay():
    ev = run("PA-1005", "dengue_medical")
    assert [(d["rule_id"], d["amount"]) for d in ev.deductions] == [("copay", 8400), ("balance_cap", 3600)]
    assert ev.eligible_amount == 30000


def test_ped_waiting_applies_only_to_related_codes():
    policy = tools.policy_by_id("GH-POL-100587")  # diabetes declared, inception 2024-03-01, 36-month PED wait
    related = rules.rule_ped_waiting(policy, ["E11.65"], date(2026, 10, 24), PED, CLAUSES)
    unrelated = rules.rule_ped_waiting(policy, ["H25.1"], date(2026, 10, 24), PED, CLAUSES)
    later = rules.rule_ped_waiting(policy, ["E11.65"], date(2027, 3, 2), PED, CLAUSES)
    assert not related.passed and related.blocking
    assert unrelated.passed and later.passed


def test_policy_period():
    policy = tools.policy_by_id("GH-POL-100231")
    assert rules.rule_policy_active(policy, date(2026, 10, 24), CLAUSES).passed
    assert not rules.rule_policy_active(policy, date(2027, 9, 12), CLAUSES).passed


def test_every_rule_has_clause_reference():
    for r in run("PA-1001", "lap_appendectomy").rules:
        assert r.clause_ref.startswith("Sec ")
