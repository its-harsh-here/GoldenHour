# Progress

## Status
| Milestone | State |
|---|---|
| M0 Scaffold, settings, LLM gateway, seed data | done |
| M1 Redaction, injection guard, intake, rules | done |
| M2 Docs/coding + risk agents, orchestrator, trace + SSE | done |
| M3 REST API, action gateway, RBAC, audit chain, e2e | next |
| M4 Console UI | todo |
| M5 Dashboard, audit tab, autonomy, guided demo, landing | todo |
| M6 Groq live verification + replay cache | todo |
| M7 Visual QA, README, DEMO_SCRIPT | todo |

## Done
- M0: `run.py`, `core/settings.py` (.env via python-dotenv), `core/store.py` (SQLite), `core/llm.py` (groq/mock/replay, per-model token bucket, SQLite response cache, retry-after on 429, pydantic validation with one retry), seed data (6 policies, 16 procedures, 10 cases incl. 2 generated PDFs), vendored fonts and Chart.js.

- M1: `core/redaction.py` (names, phones, Aadhaar-like numbers, emails, policy numbers -> tokens; local rehydration), `core/guard.py` (Prompt Guard 2 score + regex fallback, sentence highlight spans, quarantine), Intake agent with pydantic-validated extraction and a deterministic form cross-check, `rules.py` (9 pure rules) with tests for all hero numbers.

- M2: Policy agent (tool calls get_policy/get_claim_history, then rules.py), Docs & Coding agent (lookup_procedure/required_docs/benchmark_rate), Risk agent (weighted signals from pack.yaml + LLM rationale), decision policy, grounded explainer (rejects unsupported rupee figures), action routing; `core/orchestrator.py` state machine with persisted state, pause/resume and failure-to-human; `core/events.py` trace log + SSE; action gateway, RBAC and hash-chained audit wired in early because the autonomous hospital query needs them.

## How to run
```
pip install -r requirements.txt
cp .env.example .env        # LLM_MODE=mock by default
LLM_MODE=mock pytest -q
python run.py               # http://127.0.0.1:8000
```

## Known issues
- Groq is reachable from the build session (proxy-injected key). Development and tests use mock mode.
