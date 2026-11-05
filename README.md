# ASA Case Study — all three problem statements

Each problem statement is built as a working, runnable process. Each one has:
- a live run view that shows each stage as it executes, with logs and data;
- a dashboard with history, status and outputs across runs;
- one-click prepared scenarios: a happy path plus edge cases.

| | Problem | Folder | Run | URL |
|---|---|---|---|---|
| PS-1 | Invoice processing: PDF → AP decision | [ps1_invoice/](ps1_invoice/) | `python ps1_invoice/app.py` | http://localhost:8001 |
| PS-2 | Vendor onboarding: submission → approved / pending / rejected | [ps2_vendor/](ps2_vendor/) | `python ps2_vendor/app.py` | http://localhost:8002 |
| PS-3 | Personalised outreach: prospect → reviewed draft | [ps3_outreach/](ps3_outreach/) | `python ps3_outreach/app.py` | http://localhost:8003 |

Each folder has its own README covering the process map, rules, edge cases, assumptions and next steps, plus a `test_scenarios.py` that runs every prepared scenario headlessly and checks the outcome.

## Setup
```
pip install -r requirements.txt
```
Python 3.10+.

**AI engine.** Everything runs without an API key; the judgement steps then use deterministic rules. To let Claude do the judgement-heavy steps, add `ANTHROPIC_API_KEY=...` to a `.env` file in this folder:
- PS-1: reading invoices
- PS-3: choosing the hook and drafting the message

The top bar of each app shows which engine is active. Every LLM step falls back to rules if the call fails, so a live demo can't break on an API hiccup.

## Shared design (`common/`)
- `runstore.py`: a run is a list of stages, each with a status, a log and structured data, persisted to SQLite while it runs. That is what drives the live view and the dashboard.
- `webapp.py`: the standard API (`/api/meta`, `/api/runs`, `/api/runs/{id}`).
- `static/shell.js` / `shell.css`: the UI shell used by all three apps (scenarios, live timeline, dashboard).
- `llm.py`: an optional Claude wrapper (structured JSON output). It is never on the critical path.

**Demo timing.** PS-1 and PS-2 runs take about 5–10 s. PS-3 does live web research (Google News, company sites, job boards, Wikipedia) and can take up to about a minute. Any source that fails or times out falls back to its cached copy in `ps3_outreach/data/cache/`, and the run log says so.

`PACE` (seconds, default 0.45) is the minimum time each stage stays visible, so the live view can be followed during a demo. Set `PACE=0` to run at full speed.
