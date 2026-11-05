# PS-3 · Prospect → Draft

A rep types a name, title and company. The process researches the company in public sources, decides what is worth mentioning (and what must never be mentioned), writes a short email that cites its sources, checks every factual claim against those sources, and puts the draft in a review queue. **Nothing is ever sent.**

## Run it

```bash
pip install fastapi uvicorn requests beautifulsoup4 python-multipart
python ps3_outreach/app.py            # http://127.0.0.1:8003
python ps3_outreach/test_scenarios.py # all scenarios + guardrail checks, offline (served from the cache)
```

Optional: put `ANTHROPIC_API_KEY=...` in a `.env` file at the repo root (or in the environment) and Claude picks the hook and writes the copy. Without a key, a rules engine does both. The run log and the top bar show which engine was used. The grounding check runs on the output of both engines.

Research mode (`RESEARCH_MODE` env var, or the dropdown in the custom form):
`auto` (default) tries live sources and falls back to the cache. `cache` never touches the network and gives a reproducible demo. `live` uses live sources only.

## Process map

```
Prospect (name, title, company, [domain], [rep notes])
 │
 1 Intake & CRM guardrails ── customer / open opp / opted out? ──► DO_NOT_CONTACT  (stop before any research)
 │   persona = CFO | finance-ops | CEO | other
 2 Resolve company identity ── Wikipedia + Wikidata website + homepage
 │   name is a disambiguation page and no domain? ──────────────► NEEDS_INPUT  (offers candidates, one-click re-run)
 │   Wikipedia hit isn't a company (e.g. "Upwind" → sailing) → ignored, logged
 3 Gather public signals ── Google News RSS (company, company+finance terms, person),
 │   Greenhouse/Lever/Ashby job boards, homepage, Wikipedia. Every fetch is logged with URL and origin (live / cache / fallback)
 4 Verify prospect still in role ── departure headlines + a named successor? ──► NEEDS_RESEARCH (no draft, suggests new target)
 5 Classify & score signals ── type + flags, cluster duplicate coverage, 0-100 score
 6 Pick the hook ── best *eligible* signal + a different-type supporting signal
 │   nothing eligible scores ≥ 45 ───────────────────────────────► role-level draft, LOW_CONFIDENCE
 │   sensitive event in last 60 days → tact mode
 7 Draft ── subject + 80-130-word email with [n] citations + LinkedIn note (persona-aware)
 8 Quality & grounding check ── claims vs. cited source, filler phrases, length, sensitive words, CTA, personalisation score
 9 Human review queue ── READY_FOR_REVIEW | REVIEW_SENSITIVE | LOW_CONFIDENCE
     drafts/<name>_<company>_<run>.md (with sources) + .eml (X-Unsent: 1, citations stripped)
     Rep can Approve / Edit (edits are re-checked; unsupported claims block approval) / Reject in the UI
```

## Signal scoring rubric

`score = 30% recency + 35% relevance to what we sell + 20% fit with this person's role + 15% source credibility`, then ×0.75 if the headline is negative or unconfirmed.

| Component | How it is measured |
|---|---|
| Recency | ≤14 days 1.0 · ≤45 0.85 · ≤90 0.65 · ≤180 0.4 · ≤365 0.2. An open job posting counts as current (0.9). |
| Relevance | By type: hiring for AP/procurement/reconciliation roles 1.0, leadership change and M&A 0.8, funding and AI/efficiency 0.75, results 0.7, expansion 0.65, product 0.35. +0.15 if the headline mentions finance or back-office terms. ×0.4 if the company is only mentioned in someone else's story. |
| Role fit | A type × persona matrix. Results matter most to a CFO, funding to a CEO, AP hiring to a controller. +0.2 if the headline names the prospect. |
| Credibility | Company or official API 1.0 · tier-1 press 0.95 · trade/business press 0.8 · unknown 0.65 · stock-content aggregators 0.5. +0.1 if 3 or more outlets carry the story. |

Never eligible as a hook, whatever the score:
- personal items (insider stock sales, pay, Form 4)
- sensitive events (layoffs, lawsuits, breaches, investigations)
- stock-price commentary
- rumours ("reportedly", questions)
- negatively framed stories
- stories where the company isn't the subject
- anything older than 9 months

Duplicate coverage of one event, such as 30 outlets reporting one CFO appointment, is merged into a single signal. The most credible and specific headline represents it.

## Scenarios (prepared in the UI, asserted in `test_scenarios.py`)

| Scenario | Kind | What it tests | Expected |
|---|---|---|---|
| David Obstler, CFO, Datadog | happy | Job board shows open *Accounts Payable* roles: a direct pain signal. Q3 results are the supporting signal. His insider stock-sale headlines are excluded. | READY_FOR_REVIEW |
| Amiram Shachar, CEO, Upwind | happy | $400M round as the hook and the Aegis acquisition as support, with a CEO-framed pitch. Wikipedia's "Upwind" (a sailing term) is rejected. "Google's acquisition of Wiz" is not treated as Upwind's deal, and the rumoured Datadog offer isn't used. | READY_FOR_REVIEW |
| Christophe Le Caillec, CFO, American Express | edge | The loudest recent story is a 5% workforce cut "to prepare for AI". An AI-automation pitch built on that would be tone-deaf. It is excluded, and a safer hook (Q3 results) is used. Tact mode drops efficiency/headcount framing, and the draft is flagged for review. | REVIEW_SENSITIVE |
| John Dietrich, CFO, FedEx | edge | Stale CRM row. News shows he left and Jamere Jackson was named CFO. No draft; the process suggests re-targeting. | NEEDS_RESEARCH |
| "Mercury", VP Finance (fictional contact) | edge | Ambiguous name: fintech, insurer, marine engines, record label… The process doesn't guess. It lists candidates with domains, and "Use this" re-runs. After a pick, headlines about the *other* Mercurys are filtered out. | NEEDS_INPUT |
| Dana Whitfield, Controller, Brightwater Provisions Co. (fictional) | edge | No press, no job board, no Wikipedia. The process writes an honest role-level draft from the rep's notes and lists what's missing. | LOW_CONFIDENCE |
| Dominic Phillips, CFO, Samsara | guardrail | The sample CRM marks Samsara as an existing customer. The run stops before research and routes to the account owner. | DO_NOT_CONTACT |

## The grounding rule

Every sentence that states a fact about the prospect's company must carry a `[n]` marker. The checker takes the sentence's numbers and proper nouns ($400M, Q3, Aegis, Accounts Payable…) and confirms each one appears in the cited source. A sentence fails if:
- a term isn't in the cited source,
- it cites a source that doesn't exist, or
- it makes an event claim or uses a number with no citation.

Failing sentences are removed and listed. Sentences without claims are allowed, such as the bridge, the seller's own description (checked against the seller profile) and the call to action. The same check re-runs when a rep edits the draft. `test_scenarios.py` includes a fabricated-claim test that the checker must strip.

## Assumptions

- **No LinkedIn.** Scraping it breaches LinkedIn's terms. The URL is stored for the rep to check by hand. Role checks use news instead.
- Public, keyless sources only: Google News RSS, Wikipedia/Wikidata, company homepage, Greenhouse/Lever/Ashby public APIs. Polite generic User-Agent, 8s timeouts plus a hard deadline.
- **Cache:** every fetch is stored normalised in `data/cache/`, keyed by query. When a live fetch fails, the run logs it visibly and uses the cached copy. Recency is then measured from the date of the research, not from today.
- The seller (**Ledgerline**) and the CRM rows are fictional sample data. People in the happy paths are real executives, and the process uses only their public roles. The ambiguous and thin cases use fictional contacts.
- Email addresses are out of scope. The `.eml` uses a placeholder address and is marked unsent.

## What I'd build next

1. An email-finder/verification step and CRM write-back (log the touch, update stale contacts automatically).
2. More signal sources: SEC 8-K/10-Q text (CFO commentary on cost and automation), earnings-call transcripts, podcasts and interviews for person-level hooks.
3. Learn the scoring weights from reply rates, and A/B test hooks by type and persona.
4. Batch mode: a CSV of 200 prospects with a review queue sorted by confidence, so the rep starts with the best drafts.
5. A sequence (follow-ups that use the *next* best signal), still rep-approved.
