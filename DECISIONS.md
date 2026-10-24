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
- Pipeline order follows CLAUDE.md (Intake -> Policy -> Docs & Coding -> Risk). The Policy agent maps the procedure package deterministically from the form's ICD-10 code; the Docs agent's LLM mapping is a cross-check that raises a coding-inconsistency signal if it disagrees.
- The rule engine reads money and ICD-10 from the structured form only; LLM extraction can lower confidence (and escalate) but never changes an amount.
- Escalation wins over a missing-documents query when both apply; the Medical Officer can still query the hospital.
- Policy deductions (room rent, sub-limit, co-pay, balance) are not "partial denials": a Processor may sign the computed eligible amount. Any amount different from it needs a Medical Officer and a written reason.
- Added `packs/health_preauth/actions.py` for the Action Agent handlers, Reporter (dashboard), case views and demo seeding, to keep `agents.py` readable.
- Non-hero cases are pre-processed at startup with the deterministic mock LLM (labelled "seeded") and backdated human sign-offs, so the dashboard shows a realistic recent history without spending Groq quota.
- The synthetic insurer is "Kestrel Mutual Health"; letters are clearly marked as synthetic.
- The audit chain records case receipt, each agent step (digest + key fields), every gateway action (allowed or blocked), human decisions and admin changes; individual tool calls stay in the trace log.
- Groq call budget used during the build: hero cases A, B, C ran live (plus one partial C run that exposed the tool-call 400); everything else used mock or the SQLite response cache.
- Groq 400 "tool call validation failed" (malformed gpt-oss tool name) is retried twice, then the agent's allow-listed tools run locally and the model gets their results in a plain JSON call. This keeps tool use governed by the same allow-list and records the degradation in the trace.
- Screenshots in docs/screenshots are taken from `LLM_MODE=replay`, so explanations and drafts are the recorded Groq outputs, not mock templates.
- Screenshots use the preinstalled Node Playwright + Chromium (`scripts/screenshots.mjs`) instead of `pip install playwright`, as the build environment instructs not to install a second browser.
- Alpine.js was not needed; the console is vanilla JS, so it is not vendored.
