# Progress

## Status
| Milestone | State |
|---|---|
| M0 Scaffold, settings, LLM gateway, seed data | done |
| M1 Redaction, injection guard, intake, rules | next |
| M2 Docs/coding + risk agents, orchestrator, trace + SSE | todo |
| M3 REST API, action gateway, RBAC, audit chain, e2e | todo |
| M4 Console UI | todo |
| M5 Dashboard, audit tab, autonomy, guided demo, landing | todo |
| M6 Groq live verification + replay cache | todo |
| M7 Visual QA, README, DEMO_SCRIPT | todo |

## Done
- M0: `run.py`, `core/settings.py` (.env via python-dotenv), `core/store.py` (SQLite), `core/llm.py` (groq/mock/replay, per-model token bucket, SQLite response cache, retry-after on 429, pydantic validation with one retry), seed data (6 policies, 16 procedures, 10 cases incl. 2 generated PDFs), vendored fonts and Chart.js.

## How to run
```
pip install -r requirements.txt
cp .env.example .env        # LLM_MODE=mock by default
LLM_MODE=mock pytest -q
python run.py               # http://127.0.0.1:8000
```

## Known issues
- Groq is reachable from the build session (proxy-injected key). Development and tests use mock mode.
