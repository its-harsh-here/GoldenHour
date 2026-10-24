# Progress

## Status
| Milestone | State |
|---|---|
| M0 Scaffold, settings, LLM gateway, seed data | done |
| M1 Redaction, injection guard, intake, rules | done |
| M2 Docs/coding + risk agents, orchestrator, trace + SSE | done |
| M3 REST API, action gateway, RBAC, audit chain, e2e | done |
| M4 Console UI | done |
| M5 Dashboard, audit tab, autonomy, guided demo, landing | done |
| M6 Groq live verification + replay cache | done |
| M7 Visual QA, README, DEMO_SCRIPT | done |

Stretch: a `Dockerfile` (replay mode by default, `$PORT`, non-root) was added after M7; its runtime settings were verified by running the app with the same environment, but the image was not built in the build session (no Docker daemon). The second `motor_claim` pack was not started.

## Done
- M0: `run.py`, `core/settings.py` (.env via python-dotenv), `core/store.py` (SQLite), `core/llm.py` (groq/mock/replay, per-model token bucket, SQLite response cache keyed by sha256(model + messages), retry-after on 429, pydantic validation with one retry, OpenAI-style tool loop with allow-list), seed data (6 policies, 16 procedures, 10 cases including 2 generated PDFs), vendored fonts and Chart.js.
- M1: `core/redaction.py` (names, phones, Aadhaar-like numbers, emails, policy numbers -> tokens; local rehydration), `core/guard.py` (Prompt Guard 2 score + regex fallback, sentence highlight spans, quarantine), Intake agent with pydantic-validated extraction and a deterministic form cross-check, `rules.py` (9 pure rules) with tests for all hero numbers.
- M2: Policy agent (tool calls get_policy/get_claim_history, then rules.py), Docs & Coding agent (lookup_procedure/required_docs/benchmark_rate), Risk agent (weighted signals from pack.yaml + LLM rationale), decision policy, grounded explainer (rejects unsupported rupee figures), action routing; `core/orchestrator.py` state machine with persisted state, pause/resume and failure-to-human; `core/events.py` trace log + SSE.
- M3: pack-agnostic REST API (`app/main.py`); RBAC via `X-Role` enforced in the Action Gateway (403 + audit block on every rejected attempt); audit verify / Admin tamper test / restore; HTTP e2e tests for A/B/C; `scripts/smoke_http.py` drives the same flows against a live server.
- M4: console with inbox (SLA sun-dials), live SSE agent trace, decision card (waterfall + clause-cited ledger), redaction view with "What the model saw", injection quarantine banner, role-gated approvals, outbox (paper letter, email card, phone SMS).
- M5: dashboard (count-up KPIs, turnaround vs the 60-minute line, decision split, guardrail tiles, ROI projection), Audit tab (animated verify, tamper test, restore), Admin-only autonomy selector and kill switch, presentation mode, guided demo A -> B -> C (`/console?demo=1`), landing page.
- M6: Groq reached from the cloud build session (proxy-injected key). Each hero case ran once in `groq` mode end to end with `REPLAY_RECORD=true`: gpt-oss-20b for extraction, tool calling and drafting; gpt-oss-120b (reasoning_effort low) for the risk rationale and decision explanation; Prompt Guard 2 (86M) for screening. Case C's hidden text scored 0.9965, and case A ran end to end in 7.7 s live. `replay_cache.json` (42 entries, redacted and synthetic only; checked for names, phones, policy numbers and keys) replays A, B and C with zero misses. An unrecorded case falls back to mock with `replay_miss` labels and a badge.
  - Call-shape fix found live: gpt-oss sometimes emits a malformed parallel tool call (`get_claim_history<|channel|>commentary`) and Groq returns 400. The gateway retries 400s twice; failing that, it runs the agent's allow-listed tools locally and asks once more without tools (trace step `tool_fallback`). The rejection is recorded so replay follows the same path.
- M7: headless screenshots at 1440x900 (landing, inbox, case A, case C, dashboard, audit, plus tamper test, case B query, full-page and presentation variants) taken in replay mode. I reviewed each one and fixed what was visibly wrong: kill-switch banner ignoring `hidden`, trace column overflowing its grid track, cramped card headings and document toggle, re-render restarting animations, pipeline node text overflow, zero-height live bars, audit auto-scroll hiding the result banner, and top-nav overflow at 1440 px. All were re-shot after the fixes. README.md and DEMO_SCRIPT.md are complete.

## Deployment
- Live on Render: https://goldenhour-7xuc.onrender.com (`LLM_MODE=groq`, key set in Render). Verified after deploy: 10 cases, 42 replay entries, all console views render, SSE trace streams through Render's proxy, case A ran on live Groq in 10 s and was approved from the console with letter, email and SMS in the outbox.
- First deploy served an empty console: `.gitignore`'s `data/` also matched `packs/health_preauth/data/`, so the pack's data was never committed. Fixed by ignoring `/data/` only, committing the 15 data files, a packaging test that fails if pack data is untracked, and a case count in `/api/health`.
- `render.yaml` blueprint (Render, Docker, free plan, auto-deploy from `main`, `LLM_MODE=groq`). The owner applies it with the Deploy to Render link in README.md and enters `GROQ_API_KEY` in Render's prompt. The build session had no Render credentials, so the service itself was not created from here.

## Post-launch changes
- Dataset: 16 policies, 24 procedures, 6 hospitals, 40 cases. PA-1011 to PA-1040 come from the deterministic generator; their outcomes are decided at startup by the same desk logic the live feed uses.
- Live feed: `core/feed.py` thread adds a synthetic request about every 40 s, processed on arrival (mock pipeline by default); a simulated desk signs live-feed cases off within 1-7 minutes; `/api/stream` pushes desk events to the console (ticker, inbox, badges, dashboard, audit pill); retention keeps the newest 40 feed cases.
- Console: the demo button is a 20 px icon-only ▶; L1/L2/L3 and the power button are always clickable and switch the demo role to Admin, since they are Admin controls; inbox filter chips; live chips on feed cases.
- Replay cache re-recorded on live Groq (42 entries; A, B, C replay with zero misses). The first re-record hit a transient Groq 500 from Prompt Guard on case B: the guard degraded to regex as designed, and a second pass recorded it.
- Tests: 71 (generator, feed, simulated desk, retention, desk stream, feed RBAC).

## How to run
```
pip install -r requirements.txt
cp .env.example .env              # LLM_MODE=mock by default
LLM_MODE=mock pytest -q           # 71 tests, no network
python run.py                     # http://127.0.0.1:8000  -> / and /console
python scripts/smoke_http.py      # HTTP checks for hero cases A/B/C + audit (server must be running)
LLM_MODE=replay python run.py     # recorded Groq runs, for the demo video
LLM_MODE=groq python run.py       # live; needs GROQ_API_KEY in .env (REPLAY_RECORD=true to re-record)
node scripts/screenshots.mjs      # regenerate docs/screenshots (needs Playwright + Chromium)
```

## Verified in the build session
- `LLM_MODE=mock pytest -q`: 61 passed.
- Live server, mock, replay and groq modes: A approved after Processor sign-off with letter, hospital notice and SMS in the outbox; B raised a hospital query (SLA paused), then was approved at ₹36,000 after the simulated upload; C escalated with the injection flagged, and a Processor approve returned 403; the Medical Officer's denial with a reason was accepted.
- Audit verify returned valid; the Admin tamper test was detected at the edited block; restore made the chain valid again.

## Known issues
- Groq free-tier limits (about 30 RPM / 8K TPM per model) mean a live groq run of one case takes 5-60 s depending on throttling; use `LLM_MODE=replay` for the recorded demo.
- If `pytest` on your PATH is an isolated tool install (e.g. `uv tool install pytest`), it cannot see the project's packages; install `requirements.txt` into that environment or run `LLM_MODE=mock python -m pytest -q`.
- Demo RBAC trusts the `X-Role` header (no real authentication).
- Changing any prompt in `packs/health_preauth/prompts/` changes the replay keys; re-record with `LLM_MODE=groq REPLAY_RECORD=true` and `scripts/smoke_http.py`.
- On narrow phone screens the console stacks into one column and stays usable, but it is designed for 1280-1920 px desks.
