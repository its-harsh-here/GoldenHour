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
| M7 Visual QA, README, DEMO_SCRIPT | next |

## Done
- M0: `run.py`, `core/settings.py` (.env via python-dotenv), `core/store.py` (SQLite), `core/llm.py` (groq/mock/replay, per-model token bucket, SQLite response cache, retry-after on 429, pydantic validation with one retry), seed data (6 policies, 16 procedures, 10 cases incl. 2 generated PDFs), vendored fonts and Chart.js.

- M1: `core/redaction.py` (names, phones, Aadhaar-like numbers, emails, policy numbers -> tokens; local rehydration), `core/guard.py` (Prompt Guard 2 score + regex fallback, sentence highlight spans, quarantine), Intake agent with pydantic-validated extraction and a deterministic form cross-check, `rules.py` (9 pure rules) with tests for all hero numbers.

- M2: Policy agent (tool calls get_policy/get_claim_history, then rules.py), Docs & Coding agent (lookup_procedure/required_docs/benchmark_rate), Risk agent (weighted signals from pack.yaml + LLM rationale), decision policy, grounded explainer (rejects unsupported rupee figures), action routing; `core/orchestrator.py` state machine with persisted state, pause/resume and failure-to-human; `core/events.py` trace log + SSE; action gateway, RBAC and hash-chained audit wired in early because the autonomous hospital query needs them.

- M3: pack-agnostic REST API (`app/main.py`) over core + pack hooks; RBAC via `X-Role` enforced in the Action Gateway (403 + audit block on every rejected attempt); audit verify / Admin tamper test / restore; SSE trace stream; HTTP e2e tests for A/B/C; `scripts/smoke_http.py` drives the same flows against a live server.

- M4: console (`/console`): inbox with live SLA sun-dials, case view with live SSE agent trace, decision card (outcome, grounded summary, waterfall, clause-cited rule ledger), redaction view with "What the model saw" toggle, injection quarantine banner, role-gated approvals (one-click at level 2, manual at level 1, Medical Officer amount/deny with mandatory reason, Processor 403 shown as a toast), outbox (paper letter, email card, phone SMS). Vendored fonts, Chart.js and a hand-drawn icon sprite; no CDN.

- M5: dashboard (count-up serif KPIs, turnaround chart against the 60-minute line, decision split donut, autonomous-query / injection / 403 / deduction tiles, ROI projection with pack.yaml assumptions, baseline comparison); Audit tab (linked hash blocks, animated Verify chain, Admin-only tamper test that breaks at one block, Restore); autonomy selector + kill switch banner (Admin only); presentation mode (paper theme, larger type); guided demo A -> B -> C with caption toasts and automatic role switches (`/console?demo=1`); landing page with hero sun-dial, problem stats with sources, animated agent pipeline, 8 guardrails, core/packs reuse diagram.

- M6: Groq reached from the cloud build session (proxy-injected key). Each hero case ran once in `groq` mode end to end (`scripts/smoke_http.py` with `REPLAY_RECORD=true`): gpt-oss-20b for extraction, tool-calling and drafting, gpt-oss-120b (reasoning_effort low) for risk rationale and the decision explanation, Prompt Guard 2 (86M) for injection screening (case C hidden text scored 0.9965). Case A ran end to end in 7.7 s live. `replay_cache.json` (42 entries, redacted and synthetic only, checked for names, phones, policy numbers and keys) replays A, B and C with zero misses; an unrecorded case falls back to mock with `replay_miss` labels and a badge.
- Call-shape fix found live: gpt-oss sometimes emits a malformed parallel tool call (`get_claim_history<|channel|>commentary`) and Groq returns 400. The gateway now retries 400s twice and, failing that, runs the agent's allow-listed tools locally and asks once more without tools (trace step `tool_fallback`); the rejection is recorded so replay follows the same path.

## How to run
```
pip install -r requirements.txt
cp .env.example .env        # LLM_MODE=mock by default
LLM_MODE=mock pytest -q
python run.py               # http://127.0.0.1:8000
python scripts/smoke_http.py  # HTTP checks for hero cases A/B/C + audit (server must be running)
```

## Known issues
- Groq free-tier limits (about 30 RPM / 8K TPM per model) mean a live groq run of one case takes 5-60 s depending on throttling; use `LLM_MODE=replay` for the recorded demo.
- Development and tests use mock mode; `LLM_MODE=mock pytest -q` needs no network.
