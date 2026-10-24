from core.redaction import Redactor, contains_phi, rehydrate
from packs.health_preauth import agents, tools

TEXT = ("Patient: Aarav Menon, +91 98450 21734, aarav.menon@example.com. Aadhaar 4821 7730 5196. "
        "Policy no. GH-POL-100231. Mr. Menon was seen by Dr. Kavya Raman.")


def make() -> Redactor:
    return Redactor([("PATIENT", "Aarav Menon"), ("DOCTOR", "Kavya Raman")], [agents.POLICY_PATTERN])


def test_all_phi_kinds_become_tokens():
    out = make().redact(TEXT)
    for token in ("[PATIENT_1]", "[PHONE_1]", "[EMAIL_1]", "[AADHAAR_1]", "[POLICY_1]", "Dr. [DOCTOR_1]", "Mr. [PATIENT_1]"):
        assert token in out.text
    for leaked in ("Aarav", "Menon", "98450", "4821", "GH-POL", "example.com", "Kavya"):
        assert leaked not in out.text


def test_spans_point_at_original_text():
    out = make().redact(TEXT)
    for span in out.spans:
        assert TEXT[span.start : span.end] == span.text


def test_tokens_are_stable_across_documents():
    r = make()
    first = r.redact("Call +91 98450 21734 about Aarav Menon")
    second = r.redact("Aarav Menon, phone 98450 21734")
    assert "[PHONE_1]" in first.text and "[PHONE_1]" in second.text
    assert "[PHONE_2]" not in second.text


def test_rehydrate_round_trip():
    r = make()
    text = "Aarav Menon (GH-POL-100231) at aarav.menon@example.com"
    assert rehydrate(r.redact(text).text, r.mapping) == text


def test_hospital_landline_is_not_redacted():
    out = make().redact("Hospital desk +91 80 4110 2290")
    assert out.text == "Hospital desk +91 80 4110 2290"


def test_hero_notes_leave_no_phi_for_the_model():
    for case_id in ("PA-1001", "PA-1002", "PA-1003"):
        req = tools.load_case(case_id)
        r = agents.build_redactor(req)
        out = r.redact(tools.note_text(req))
        assert not contains_phi(out.text, r.mapping), case_id
        assert "[PATIENT_1]" in out.text and "[POLICY_1]" in out.text
