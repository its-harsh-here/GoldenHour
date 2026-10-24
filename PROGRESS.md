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
| M6 Live LLM verification + replay cache | done |
| M7 Visual QA, README, DEMO_SCRIPT | done |

Stretch: a `Dockerfile` (replay mode by default, `$PORT`, non-root) and a Render blueprint (`render.yaml`). The second `motor_claim` pack was not started.

## Done
- M0: `run.py`, `core/settings.py` (.env via python-dotenv), `core/store.py` (SQLite), `core/llm.py` (live/mock/replay, per-role token bucket, SQLite response cache keyed by sha256(role + messages), retry-after on 429, pydantic validation with one retry, OpenAI-style tool loop with allow-list), seed data, vendored fonts and Chart.js.
- M1: `core/redaction.py` (names, phones, Aadhaar-like numbers, emails, policy numbers -> tokens; local rehydration), `core/guard.py` (guard-model score + regex fallback, sentence highlight spans, quarantine), Intake agent with pydantic-validated extraction and a deterministic form cross-check, `rules.py` (9 pure rules) with tests for all hero numbers.
- M2: Policy agent (tool calls get_policy/get_claim_history, then rules.py), Docs & Coding agent (lookup_procedure/required_docs/benchmark_rate), Risk agent (weighted signals from pack.yaml + LLM rationale), decision policy, grounded explainer (rejects unsupported rupee figures), action routing; `core/orchestrator.py` state machine with persisted state, pause/resume and failure-to-human; `core/events.py` trace log + SSE.
- M3: pack-agnostic REST API (`app/main.py`); RBAC via `X-Role` enforced in the Action Gateway (403 + audit block on every rejected attempt); audit verify / Admin tamper test / restore; HTTP e2e tests for A/B/C; `scripts/smoke_http.py` drives the same flows against a running server.
- M4: console with inbox (SLA sun-dials), live SSE agent trace, decision card (waterfall + clause-cited ledger), redaction view with "What the model saw", injection quarantine banner, role-gated approvals, outbox (paper letter, email card, phone SMS).
- M5: dashboard (count-up KPIs, turnaround vs the 60-minute line, decision split, guardrail tiles, ROI projection), Audit tab (animated verify, tamper test, restore), Admin-only autonomy selector and kill switch, presentation mode, guided demo A -> B -> C (`/console?demo=1`), landing page.
- M6: each hero case ran end to end in `live` mode with `REPLAY_RECORD=true`: the fast model for extraction, tool calling and drafting; the reasoning model (reasoning_effort low) for the risk rationale and decision explanation; the guard model for injection screening. Case C's hidden text scored 0.9965. `replay_cache.json` (42 entries, keyed by model role, redacted and synthetic only) replays A, B and C with zero misses, including the full guided demo. An unrecorded case falls back to mock with `replay_miss` labels and a badge.
  - Call-shape fix found live: the fast model sometimes emits a malformed parallel tool call and the provider returns 400. The gateway retries 400s twice; failing that, it runs the agent's allow-listed tools locally and asks once more without tools. The rejection is recorded so replay follows the same path.
- M7: headless screenshots at 1440x900 in replay mode, reviewed one by one and re-shot after fixes. README.md and DEMO_SCRIPT.md are complete.

## Deployment
- Live on Render: https://goldenhour-7xuc.onrender.com, from `render.yaml` (Docker, free plan, auto-deploy from `main`, health check `/api/health`).
- The service runs `LLM_MODE=live` once `LLM_BASE_URL`, `LLM_API_KEY`, `MODEL_FAST`, `MODEL_REASON` and `MODEL_GUARD` are set in Render's dashboard (never in the repo). Until then, or with any unknown mode, it serves the recorded runs (`replay`) and the header badge says so.
- An early deploy served an empty console because `.gitignore`'s `data/` also matched the pack's data folder. Fixed by ignoring `/data/` only, plus a packaging test and a case count in `/api/health`.

## Post-launch changes
- Dataset: 16 policies, 24 procedures, 6 hospitals, 40 inbox cases; PA-1011 to PA-1040 come from the deterministic generator.
- Live feed (`core/feed.py`): requests arrive at random (a Poisson process, mean `FEED_INTERVAL_S`, default 40 s), land as New and are picked up by the agents 3–8 s later. Up to `FEED_LIVE_PER_HOUR` (6) an hour run on the live model in live mode, the rest on the simulator with model-like timing. A simulated desk (`sim:*` actors) signs generated cases off within minutes. `/api/stream` pushes desk events to the console.
- A day of case solving: at startup (and after a demo reset) the server plays the last 24 hours on a simulated business clock (`core/clock.py`): about 200 requests shaped by an IST intraday profile (`DAY_VOLUME`), each processed by the agent pipeline and worked by the simulated desk, so the dashboard opens on a full day. It takes about 5 s. "Replay the day" (inbox live panel or dashboard) plays the same day paced over `DAY_REPLAY_MINUTES` (default 3), with a moving clock, progress bar, ticker and an hour-by-hour chart that fills in. Every timestamp (case, trace, audit, notification) is on the simulated timeline; open replay cases show SLA on the replay's clock.
- Smooth UI: tab, role, filter, document-view and theme changes cross-fade with the View Transitions API (skipped under reduced motion). Live updates patch the DOM in place (keyed rows, appended trace lines, in-place chart updates), so nothing re-renders or replays its entrance animation. Refreshes during a replay are throttled rather than debounced, so screens keep moving while events stream.
- De-branding: the provider and model names are gone from code, config and docs. Agents ask for a role (`fast`, `reason`, `guard`) and env maps roles to model ids. the provider-named mode became `live`, and `LLM_REASONING_EFFORT` makes the reasoning hint optional. The landing page and console footer credit the builder (github.com/its-harsh-here).
- Autonomy L3 fix: L3 used to look like L1, for three reasons. The sign-off panel offered the pre-filled one-click approval only at exactly level 2, so L3 fell through to L1's manual form and its "Autonomy level 1" text. Switching levels changed nothing already in the queue. And the Admin view (clicking a level switches to Admin) said "read-only" at every level. Now L2 and L3 both offer one-click sign-off and only L1 is manual. Switching to L3 auto-executes the clean cases waiting for a Processor (trace step `auto_execute`, 10% post-audit sample). Toasts and the read-only panel spell out what each level does. Verified in the browser in replay mode: case A and four waiting day cases auto-executed with zero replay misses.
- One header indicator and new cases that arrive New: the header had a "Live" stream pill next to the LLM mode badge, which also read LIVE once Render ran live, so that pill, the inbox "Live feed" pill and the dashboard "Feed on" pill (all pulsing dots) are gone; the header keeps only the plain-text LLM mode badge. Feed arrivals used to arrive already processed; now they land as New, the agents pick them up within seconds, a few an hour run on the live model and the rest on the simulator with model-like timing (`run_mode` column, "Live model" chip, "picked up by the agents" ticker line). Verified locally in live mode: PA-2001 ran on the live model, PA-2002..2013 on the simulator, each New -> Agents working -> settled in the inbox, trace streaming step by step.
- Final pre-release scan: removed the landing page's "Run the 3-case demo" buttons (the console's ▶ and `/console?demo=1` still work). Fixed a race where starting a day replay while the agents were mid-run on a new arrival crashed that run in the background: a replay now waits for in-flight arrivals, pickups never start while the desk is being cleared, and a run whose case disappears stops quietly. Also fixed two pyflakes warnings. Verified locally: HTTP smoke for A/B/C and the audit chain, the guided demo with zero replay misses, every console control, phone-width layouts, no console errors, no failed requests, no server errors.
- Tests: 87 (a case removed mid-run stops quietly; arrive-as-New and pickup, the live-model share and hourly cap, simulated timing on a simulated clock; autonomy levels L1/L2/L3 and the kill switch over HTTP; adds the business clock, day replay, replay-clock SLA, light case list, hour-by-hour dashboard, day API, live-mode settings, the reasoning-effort switch, and leftover replay cases keeping their planned timing).

## How to run
```
pip install -r requirements.txt
cp .env.example .env              # LLM_MODE=mock by default
LLM_MODE=mock pytest -q           # 87 tests, no network
python run.py                     # http://127.0.0.1:8000  -> / and /console
python scripts/smoke_http.py      # HTTP checks for hero cases A/B/C + audit (server must be running)
LLM_MODE=replay python run.py     # recorded live runs, for the demo video
LLM_MODE=live python run.py       # needs LLM_BASE_URL, LLM_API_KEY, MODEL_FAST/REASON/GUARD in .env
node scripts/screenshots.mjs      # regenerate docs/screenshots (needs Playwright + Chromium)
```

## Verified in the build session
- `LLM_MODE=mock pytest -q`: 87 passed.
- Replay mode with the feed and the day backfill running: hero cases A, B, C reached their end states over HTTP (`scripts/smoke_http.py`) with zero replay misses. The guided demo ran headless in 69 s, also with zero misses; the only console error is case C's intended 403.
- Day replay in the browser: backfill of 192 requests (398 simulated events) in about 5 s; a paced replay filled the hour-by-hour chart in place (bar totals 56 -> 163 over 24 s) without rebuilding the inbox or the dashboard; the audit chain verifies afterwards.
- Audit verify returned valid; the Admin tamper test was detected at the edited block; restore made the chain valid again.

## Known issues
- Free-tier provider limits (often about 30 RPM / 8K TPM per model) mean a live run of one case takes 5-60 s under throttling, and back-to-back hero cases can hit a 429 (the case then goes to a human with the reason). Use `LLM_MODE=replay` for the recorded demo.
- Only up to `FEED_LIVE_PER_HOUR` live-feed arrivals an hour use the live model; day-replay, history and the other feed cases run on the simulator, because a day is about 200 cases, far beyond a free tier. At the default of 6 an hour, the feed uses roughly 40K fast-model tokens an hour while the server is awake.
- If `pytest` on your PATH is an isolated tool install (e.g. `uv tool install pytest`), it cannot see the project's packages; install `requirements.txt` into that environment or run `LLM_MODE=mock python -m pytest -q`.
- Demo RBAC trusts the `X-Role` header (no real authentication).
- Changing any prompt in `packs/health_preauth/prompts/` changes the replay keys; re-record with `LLM_MODE=live REPLAY_RECORD=true` and `scripts/smoke_http.py`.
- Free Render instances sleep when idle and keep SQLite in `/tmp`, so the first request after a pause takes 30-60 s and the desk (with its fresh 24-hour backfill) restarts on every deploy.
- On narrow phone screens the console stacks into one column and stays usable, but it is designed for 1280-1920 px desks.
