# Decisions log

One line per judgement call made during the unattended build.

- Python 3.13 is what the build container has; code targets 3.11+ and uses nothing newer.
- Groq is reachable from the cloud session through a proxy-injected key, so the SDK gets a placeholder key when `GROQ_API_KEY` is empty.
- Prompt Guard 2 on Groq returns the malicious-probability as a bare string (e.g. `"0.9986"`); we parse it as a float and treat ≥ 0.5 as an injection, with a regex fallback that also supplies highlight spans.
- Added `core/store.py` (one SQLite connection behind a lock) even though the layout in CLAUDE.md does not list it; every core module needs persistence and this keeps it in one place.
- The replay key and the SQLite response cache key are both `sha256(model + json(messages))`; prompts contain no timestamps so recorded runs replay byte-for-byte.
- Replay recording only happens when `REPLAY_RECORD=true`, so a laptop run in groq mode never rewrites the committed `replay_cache.json` by accident.
- Mock fixtures live next to each agent (a function of the agent's own inputs), so mock, replay-miss fallback and tests all go through the same `run_agent` code path as groq.
- Hero cases are PA-1001 (A), PA-1002 (B), PA-1003 (C); case IDs are short and readable for the demo.
- Case C's admission note is a generated PDF with the injection printed in white 1pt text: invisible on paper, present in the extracted text (the realistic attack).
- Estimates use six heads (room, OT, surgeon, consumables, medicines, investigations); the structured form is the money source of truth and the LLM's note extraction is only a cross-check that can lower confidence.
- Pre-existing-disease relevance is decided deterministically by ICD-10 prefix (policies.json `ped_icd10_prefixes`); the LLM can only add an advisory "possible PED link" risk signal.
