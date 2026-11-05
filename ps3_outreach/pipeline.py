"""PS-3 — Personalised outreach: from prospect to a reviewed, grounded draft.

Process map (decision points in CAPS):
  1 intake    Intake & CRM guardrails      -> DO_NOT_CONTACT if customer / open opp / opted out
  2 resolve   Resolve company identity     -> NEEDS_INPUT if the name is ambiguous and nothing disambiguates it
  3 gather    Gather public signals        (news, job boards, website, Wikipedia; every item logged with URL)
  4 role      Verify prospect still in role-> NEEDS_RESEARCH if public sources say they left / were replaced
  5 score     Classify & score signals     (type, flags, 0-100 score; sensitive/personal items excluded)
  6 hook      Pick the hook                -> LOW_CONFIDENCE if no usable company-specific signal
  7 draft     Draft the message            (Claude if available, else rules; same output contract)
  8 qa        Quality & grounding check    (claims must trace to a cited source; unsupported ones are cut)
  9 review    Human review queue           -> READY_FOR_REVIEW / REVIEW_SENSITIVE / LOW_CONFIDENCE
Nothing is ever sent. The output is a draft file for a rep to approve or edit.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from common import llm  # noqa: E402

import drafting  # noqa: E402
import signals as S  # noqa: E402
from research import Research, age_days, domain_of, parse_iso  # noqa: E402

DATA = ROOT / "data"
DRAFTS = ROOT / "drafts"

PLAN = [
    ("intake", "Intake & CRM guardrails"),
    ("resolve", "Resolve company identity"),
    ("gather", "Gather public signals"),
    ("role", "Verify prospect is still in role"),
    ("score", "Classify & score signals"),
    ("hook", "Pick the hook"),
    ("draft", "Draft the message"),
    ("qa", "Quality & grounding check"),
    ("review", "Queue for human review"),
]

DECISIONS = {
    "READY_FOR_REVIEW": "good",
    "REVIEW_SENSITIVE": "warn",
    "LOW_CONFIDENCE": "warn",
    "NEEDS_INPUT": "info",
    "NEEDS_RESEARCH": "warn",
    "DO_NOT_CONTACT": "bad",
}

SCENARIOS = [
    {"id": "datadog-cfo", "kind": "happy", "expect": "READY_FOR_REVIEW",
     "title": "CFO at a public SaaS company — Datadog",
     "description": "David Obstler, CFO of Datadog. Real news (Q3 call) plus live job-board data: Datadog is hiring Accounts Payable roles — a direct pain signal for an AP-automation seller.",
     "input": {"name": "David Obstler", "title": "Chief Financial Officer", "company": "Datadog", "domain": "datadoghq.com"}},
    {"id": "upwind-ceo", "kind": "happy", "expect": "READY_FOR_REVIEW",
     "title": "Founder-CEO after a big raise — Upwind",
     "description": "Amiram Shachar, CEO of Upwind. Fresh Series C plus an acquisition; persona is a CEO, so the draft argues scale, not close-cycle detail. Wikipedia's 'Upwind' is a sailing term — the resolver must notice.",
     "input": {"name": "Amiram Shachar", "title": "CEO & Co-founder", "company": "Upwind", "domain": "upwind.io"}},
    {"id": "amex-sensitive", "kind": "edge", "expect": "REVIEW_SENSITIVE",
     "title": "Loudest signal is sensitive — American Express",
     "description": "Christophe Le Caillec, CFO. The biggest recent story is a 5% workforce cut 'to prepare for AI'. Pitching AI automation off that would be tone-deaf: the process must suppress it, pick a safer hook and flag for review.",
     "input": {"name": "Christophe Le Caillec", "title": "Chief Financial Officer", "company": "American Express", "domain": "americanexpress.com"}},
    {"id": "fedex-role-change", "kind": "edge", "expect": "NEEDS_RESEARCH",
     "title": "Stale CRM record — prospect has left (FedEx)",
     "description": "Rep's list says John Dietrich is FedEx's CFO. Public news says he left and FedEx named a new CFO. The process must stop and suggest re-targeting instead of drafting to the wrong person.",
     "input": {"name": "John Dietrich", "title": "Chief Financial Officer", "company": "FedEx", "domain": "fedex.com"}},
    {"id": "mercury-ambiguous", "kind": "edge", "expect": "NEEDS_INPUT",
     "title": "Ambiguous company name — 'Mercury'",
     "description": "A list row with just 'Mercury' and no domain: fintech? insurer? boat engines? defence? The process must not guess. It stops and asks the rep to pick, offering one-click re-runs.",
     "input": {"name": "Taylor Brooks", "title": "VP Finance", "company": "Mercury", "domain": "", "context": "Fictional contact name; company left ambiguous on purpose."}},
    {"id": "thin-signal", "kind": "edge", "expect": "LOW_CONFIDENCE",
     "title": "Thin signal — small private company",
     "description": "A small private distributor with no press, no public job board, no Wikipedia page (fictional company and person). The process must not fake specificity: role-level draft, LOW_CONFIDENCE, and a list of what's missing.",
     "input": {"name": "Dana Whitfield", "title": "Controller", "company": "Brightwater Provisions Co.", "domain": "", "context": "regional specialty-food distributor, about 200 employees"}},
    {"id": "crm-customer", "kind": "edge", "expect": "DO_NOT_CONTACT",
     "title": "Already a customer — CRM guardrail",
     "description": "Dominic Phillips, CFO of Samsara. The (sample) CRM shows Samsara is an existing customer, so cold outreach would collide with the account team. Stops before any research is spent.",
     "input": {"name": "Dominic Phillips", "title": "Chief Financial Officer", "company": "Samsara", "domain": "samsara.com"}},
]

COMPANY_RX = re.compile(r"\b(company|companies|corporation|conglomerate|business|firm|bank|insurer|insurance|manufacturer|maker|brand|retailer|startup|fintech|provider|platform|subsidiary|holding|airline|carrier|label|developer|services|technology|software|multinational|enterprise|contractor)\b", re.I)
DEPART_RX = re.compile(r"\bformer\b|\bex-|outgoing|\bdeparts?\b|departing|departure|steps? down|stepping down|resign|retir|\bleaves\b|leaving|farewell|\bexit|successor|joins .*board|elects? .* to (its|the) board|separation", re.I)


def load_seller(overrides: dict | None = None) -> dict:
    seller = json.loads((DATA / "seller_profile.json").read_text(encoding="utf-8"))
    for k, v in (overrides or {}).items():
        if v:
            seller[k] = v.strip()
    return seller


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(inc|corp|corporation|co|company|ltd|llc|plc|the|group|holdings)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def crm_lookup(name: str, company: str, domain: str) -> list[dict]:
    hits = []
    with open(DATA / "crm.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["contact_name"] and _norm(row["contact_name"]) == _norm(name):
                hits.append({**row, "match": "person"})
            elif row["account"] and (_norm(row["account"]) == _norm(company) or (domain and row["domain"] and domain_of(row["domain"]) == domain)):
                hits.append({**row, "match": "account"})
    return hits


def _log_fetch(st, label: str, data, meta: dict, summary: str = ""):
    o = meta["origin"]
    if o == "live":
        st.log(f"[live] {label}{(' → ' + summary) if summary else ''}  ·  {meta['url']}", "info")
    elif o == "cache":
        st.log(f"[cache {str(meta['fetched_at'])[:10]}] {label}{(' → ' + summary) if summary else ''}", "info")
    elif o == "fallback":
        st.log(f"[cache fallback] {label}: live fetch failed ({meta['error']}); using copy cached {str(meta['fetched_at'])[:16]}{(' → ' + summary) if summary else ''}", "warn")
    else:
        st.log(f"[unavailable] {label}: {meta['error']}", "warn")


def _slugs(company: str, domain: str) -> list[str]:
    base = _norm(company)
    out = []
    for s in (base.replace(" ", ""), base.replace(" ", "-"), (domain.split(".")[0] if domain else "")):
        if s and s not in out and len(s) >= 3:
            out.append(s)
    return out[:3]


def _board_matches(company: str, slug: str, data: dict) -> bool:
    name = data.get("board_name") or ""
    if name:
        return _norm(name).replace(" ", "") in (_norm(company).replace(" ", ""), slug) or _norm(company).replace(" ", "") in _norm(name).replace(" ", "")
    return slug == _norm(company).replace(" ", "") or slug == _norm(company).replace(" ", "-")


SECTOR_WORDS = {  # extra words that give away a *different* company with the same name
    "insurance": ["insurance", "insurer", "homeowners", "policyholders", "premiums", "auto policies"],
    "record": ["album", "music", "records", "singer", "artist", "single"],
    "automobile": ["car", "cars", "sedan", "ford"],
    "marine": ["boat", "boats", "outboard", "marine"],
    "aviation": ["aircraft", "airline", "aviation"],
    "defense": ["defense", "defence", "pentagon", "missile"],
}
GENERIC = {"american", "company", "companies", "organization", "organisation", "united", "states", "based", "corporation", "multinational", "brand", "former", "defunct", "california", "inc", "the", "and", "of"}


def _candidates(R, st, company: str) -> list[dict]:
    res, m3 = R.wiki_search(f"{company} company", limit=15)
    _log_fetch(st, f"Wikipedia search “{company} company”", res, m3, f"{len(res or [])} results")
    cands = []
    for p in res or []:
        if _norm(company).split()[0] in _norm(p["title"]) and COMPANY_RX.search(p.get("description", "")):
            site = ""
            if p.get("wikibase_item"):
                site, m4 = R.wikidata_website(p["wikibase_item"])
                _log_fetch(st, f"Wikidata website for {p['title']}", site, m4, site or "none")
            cands.append({"title": p["title"], "description": p.get("description", ""), "domain": domain_of(site or "")})
        if len(cands) >= 8:
            break
    return cands


def _sibling_terms(company: str, chosen_title: str, chosen_desc: str, cands: list[dict]) -> tuple[list[str], list[str]]:
    """Words that mark a headline as being about a *different* same-name company, and words that fit the chosen one."""
    base = set(_norm(company).split())
    mine = set(re.findall(r"[a-z]{4,}", (chosen_title + " " + chosen_desc).lower())) - GENERIC - base
    excl = set()
    for c in cands:
        if _norm(c["title"]) == _norm(chosen_title):
            continue
        words = set(re.findall(r"[a-z]{4,}", (c["title"] + " " + c["description"]).lower())) - GENERIC - base - mine
        excl |= words
        for k, extra in SECTOR_WORDS.items():
            if k in c["description"].lower() or k in c["title"].lower():
                excl |= set(extra) - mine
    excl -= {"technology", "technologies", "financial", "services"} & mine
    return sorted(excl), sorted(mine)


def _stop(decision: str, summary: str, **extra) -> dict:
    return {"decision": decision, "summary": summary, "engine": llm.engine_name(), **extra}


# =============================================================================== pipeline
def pipeline(run, inp: dict) -> dict:
    prospect = {"name": (inp.get("name") or "").strip(), "title": (inp.get("title") or "").strip(),
                "company": (inp.get("company") or "").strip(), "domain": domain_of(inp.get("domain") or ""),
                "linkedin": (inp.get("linkedin") or "").strip()}
    rep_context = (inp.get("context") or "").strip()
    seller = load_seller(inp.get("seller"))
    R = Research(inp.get("research_mode"))
    company = prospect["company"]
    base = {"prospect": prospect, "seller": seller, "research_mode": R.mode}

    # ------------------------------------------------------------------ 1. intake
    with run.stage("intake", "Intake & CRM guardrails") as st:
        missing = [k for k in ("name", "title", "company") if not prospect[k]]
        if missing:
            st.status("fail", "Missing required fields: " + ", ".join(missing))
            return _stop("NEEDS_INPUT", f"Missing required input: {', '.join(missing)}.", **base, next_steps=["Fill in the missing fields and re-run."])
        persona = S.persona_of(prospect["title"])
        st.log(f"Prospect: {prospect['name']} — {prospect['title']} @ {company}" + (f" ({prospect['domain']})" if prospect["domain"] else " (no domain given)"))
        st.log(f"Persona: {S.PERSONAS[persona]} — decides which signals matter and how the pitch is framed")
        if prospect["linkedin"]:
            st.log("LinkedIn URL recorded for the rep, but not fetched: scraping LinkedIn breaches its terms. Rep can skim it manually during review.", "warn")
        st.log(f"Seller: {seller['name']} — {seller['one_liner']}")
        st.log(f"Research mode: {R.mode} ({'live sources, cache fallback' if R.mode == 'auto' else 'cache only — reproducible demo' if R.mode == 'cache' else 'live only'})")
        hits = crm_lookup(prospect["name"], company, prospect["domain"])
        st.data(persona=persona, crm_matches=len(hits))
        if hits:
            h = hits[0]
            label = {"customer": "an existing customer", "open_opportunity": "an open opportunity", "do_not_contact": "on the do-not-contact list"}.get(h["status"], h["status"])
            st.log(f"CRM match ({h['match']}): {h['account'] or h['contact_name']} is {label}. Owner: {h['owner']}. {h['note']}", "error")
            st.status("fail", f"Stopped: {h['account'] or h['contact_name']} is {label} — owned by {h['owner']}")
            return _stop("DO_NOT_CONTACT", f"{h['account'] or h['contact_name']} is {label} in CRM (owner: {h['owner']}). No research or draft — route to the owner.",
                         **base, persona=persona, crm=hits,
                         next_steps=[f"Talk to {h['owner']} before any outreach to {company}.",
                                     "If this is an expansion play, the account team owns the message — share the research request with them."])
        st.log("No CRM conflicts (not a customer, no open opportunity, not opted out)", "ok")

    # ------------------------------------------------------------------ 2. resolve
    with run.stage("resolve", "Resolve company identity") as st:
        ident = {"name": company, "domain": prospect["domain"], "description": "", "extract": "", "wiki_url": None, "aliases": [], "source": "input"}
        wiki_title = (inp.get("wiki_title") or "").strip() or company
        w, meta = R.wiki_summary(wiki_title)
        _log_fetch(st, f"Wikipedia summary “{wiki_title}”", w, meta, "" if not w else ("not found" if w.get("missing") else f"{w.get('type')}: {w.get('title')} — {w.get('description')}"))
        ambiguous = False
        if w and not w.get("missing"):
            if w.get("type") == "disambiguation":
                ambiguous = True
                st.log(f"“{wiki_title}” is a disambiguation page — the name refers to several different things", "warn")
            elif COMPANY_RX.search(w.get("description", "") + " " + w.get("extract", "")[:250]) and _norm(company).split()[0] in _norm(w.get("title", "") + " " + w.get("extract", "")[:200]):
                ident.update(description=w.get("description", ""), extract=w.get("extract", ""), wiki_url=w.get("url"), source="wikipedia")
                if _norm(w["title"]) != _norm(company):
                    ident["aliases"].append(w["title"])
                qid = w.get("wikibase_item")
                if qid:
                    site, m2 = R.wikidata_website(qid)
                    _log_fetch(st, f"Wikidata official website ({qid})", site, m2, site or "none listed")
                    sd = domain_of(site or "")
                    if sd and not ident["domain"]:
                        ident["domain"] = sd
                        st.log(f"Domain inferred from Wikidata: {sd}", "ok")
                    elif sd and ident["domain"] and sd != ident["domain"] and not (sd.endswith(ident["domain"]) or ident["domain"].endswith(sd)):
                        st.log(f"Wikidata lists {sd} but the rep gave {ident['domain']} — keeping the rep's domain, flagging for review", "warn")
                        ident["domain_conflict"] = sd
            else:
                st.log(f"Wikipedia's “{w.get('title')}” is “{w.get('description') or 'not a company'}” — not this company; ignored", "warn")
        if ambiguous:
            cands = _candidates(R, st, company)
            st.data(candidates=cands)
            chosen = next((c for c in cands if prospect["domain"] and c["domain"] and (c["domain"] == prospect["domain"] or c["domain"].endswith("." + prospect["domain"]))), None)
            if chosen:
                st.log(f"Domain {prospect['domain']} matches {chosen['title']} ({chosen['description']}) — ambiguity resolved", "ok")
                ident.update(description=chosen["description"], aliases=[chosen["title"]], source="wikipedia (disambiguated by domain)")
                ident["exclude_terms"], ident["context_terms"] = _sibling_terms(company, chosen["title"], chosen["description"], cands)
            elif prospect["domain"]:
                st.log(f"No Wikipedia candidate matches {prospect['domain']} — anchoring identity on the domain itself", "warn")
                ident["source"] = "domain"
            else:
                for c in cands:
                    st.log(f"Candidate: {c['title']} — {c['description']}" + (f" ({c['domain']})" if c["domain"] else ""))
                st.status("warn", f"“{company}” matches {len(cands)} different companies — need the rep to confirm which one")
                return _stop("NEEDS_INPUT", f"“{company}” is ambiguous ({len(cands)} candidate companies). Rep must confirm the company or add a domain before research continues.",
                             **base, persona=persona, candidates=cands, research=R.stats,
                             next_steps=["Pick the right company below (re-runs with its domain), or add the company domain / LinkedIn company URL to the input."])
        elif _norm(wiki_title) != _norm(company) and ident["source"] == "wikipedia":
            # rep picked a specific entity for a shared name (e.g. "Mercury" -> Mercury Technologies)
            cands = _candidates(R, st, company)
            ident["exclude_terms"], ident["context_terms"] = _sibling_terms(company, wiki_title, ident["description"], cands)
        if ident.get("exclude_terms") is not None:
            st.log(f"Shared name: headlines mentioning {', '.join(ident['exclude_terms'][:10])}… will be treated as a different “{company}”", "info")
        if ident["domain"]:
            site, m5 = R.website(ident["domain"])
            _log_fetch(st, f"Company website {ident['domain']}", site, m5, (site or {}).get("title", "")[:90] if site else "")
            if site:
                ident["site_title"], ident["site_description"] = site.get("title", ""), site.get("description", "")
                if _norm(company).split()[0] not in _norm(site.get("title", "") + " " + site.get("description", "") + " " + site.get("final_url", "")):
                    st.log("Website title doesn't mention the company name — domain may be wrong; flagged", "warn")
                    ident["domain_suspect"] = True
        st.data(identity={k: v for k, v in ident.items() if k != "extract"})
        if ident["source"] == "input" and not ident["domain"]:
            st.status("warn", "No independent source confirms who this company is")
        else:
            st.note(f"{company}" + (f" — {ident['description']}" if ident["description"] else "") + (f" · {ident['domain']}" if ident["domain"] else ""))

    # ------------------------------------------------------------------ 3. gather
    with run.stage("gather", "Gather public signals") as st:
        aliases = ident["aliases"]
        names_q = " OR ".join(f'"{n}"' for n in [company] + aliases)
        raw: list[dict] = []
        as_of = None
        if ident.get("context_terms"):
            ctx_terms = [t for t in ident["context_terms"] if t not in ("company",)][:3]
            names_q = f'"{company}" ({" OR ".join(ctx_terms + [ident["domain"] or company])})'
        queries = [(names_q, "180d", "company"), (f"({names_q}) (CFO OR finance OR acquisition OR funding OR earnings OR hiring)", "180d", "company"), (f'"{prospect["name"]}"', "365d", "person")]
        person_items = []
        for q, win, kind in queries:
            items, meta = R.news(q, win)
            _log_fetch(st, f"Google News: {q} (last {win})", items, meta, f"{len(items or [])} headlines")
            if items is not None and as_of is None:
                as_of = parse_iso(meta["fetched_at"]) if meta["origin"] != "live" else datetime.now(timezone.utc)
            for it in items or []:
                it = {**it, "query": kind}
                if kind == "person":
                    person_items.append(it)
                raw.append(it)
        as_of = as_of or datetime.now(timezone.utc)
        if (as_of - datetime.now(timezone.utc)).days < -1:
            st.log(f"Recency is measured as of the research date ({as_of.date()}), because cached results are in use", "warn")
        # keep only headlines that actually name the company; drop duplicates
        seen, news = set(), []
        dropped = sibling = 0
        for it in raw:
            key = it["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            mentions, _ = S.is_about(it["title"], company, aliases)
            if not mentions:
                dropped += 1
                continue
            if ident.get("exclude_terms") and re.search(r"\b(" + "|".join(map(re.escape, ident["exclude_terms"])) + r")\b", it["title"], re.I):
                sibling += 1
                continue
            d = age_days(it.get("published"), as_of)
            if d is not None and d > 365:
                dropped += 1
                continue
            news.append({**it, "days": d})
        st.log(f"{len(news)} distinct headlines name {company}; {dropped} dropped (other entities, duplicates or older than a year)")
        if sibling:
            st.log(f"{sibling} headline(s) were about a different “{company}” (same name, different business) and were dropped", "warn")
        # job boards
        boards = []
        slugs = _slugs(company, ident["domain"])
        for prov, slug, data, meta in R.job_boards(slugs):
            if data and data.get("exists"):
                ok = _board_matches(company, slug, data)
                _log_fetch(st, f"{prov.title()} board “{slug}”", data, meta, f"{len(data['jobs'])} open roles" + ("" if ok else f" — board belongs to “{data.get('board_name')}”, ignored"))
                if ok:
                    boards.append({"provider": prov, "slug": slug, "jobs": data["jobs"]})
            elif meta["origin"] in ("fallback", "miss") and meta.get("error") not in (None, "not in cache"):
                _log_fetch(st, f"{prov.title()} board “{slug}”", data, meta)
        if not boards:
            st.log(f"No public Greenhouse / Lever / Ashby board found for {company} (tried: {', '.join(slugs) or '—'})")
        st.data(headlines=len(news), job_boards=[f"{b['provider']}:{b['slug']} ({len(b['jobs'])} roles)" for b in boards],
                website=ident.get("site_title") or None, wikipedia=ident.get("wiki_url"), research=dict(R.stats))
        if R.stats["fallback"]:
            st.status("warn", f"{R.stats['fallback']} source(s) served from cache because the live fetch failed")

    # ------------------------------------------------------------------ 4. role
    with run.stage("role", "Verify prospect is still in role") as st:
        last = prospect["name"].split()[-1]
        role_rx = {"cfo": r"\bcfo\b|chief financial|finance chief", "ceo": r"\bceo\b|chief executive|founder", "finance_ops": r"controller|accounts payable|procurement|finance"}.get(persona, r"(?!x)x")
        departures, confirms, cooccur = [], [], 0
        for it in person_items:
            t = it["title"]
            if re.search(rf"\b{re.escape(last)}\b", t, re.I):
                if DEPART_RX.search(t):
                    departures.append(it)
                elif S.is_about(t, company, ident["aliases"])[0]:
                    confirms.append(it)
            elif S.is_about(t, company, ident["aliases"])[0]:
                cooccur += 1
        successor = None
        for it in news:
            t = it["title"]
            if re.search(role_rx, t, re.I) and re.search(r"\b(names?|named|appoints?|appointed|taps|tapped|hires?|nabs|promotes?|promoted)\b", t, re.I) and not re.search(r"form 4|interim", t, re.I) and not re.search(rf"\b{re.escape(last)}\b", t, re.I):
                who, _ = drafting._appointee(t)
                if who and who.lower() != prospect["name"].lower():
                    if not successor or (it.get("days") or 999) < (successor["item"].get("days") or 999):
                        successor = {"name": who, "item": it}
        for d in departures[:4]:
            st.log(f"Departure signal: “{d['title']}” — {d['source']}, {str(d.get('published'))[:10]}", "warn")
        if successor:
            st.log(f"Successor signal: “{successor['item']['title']}” — {successor['item']['source']}, {str(successor['item'].get('published'))[:10]}", "warn")
        for c in confirms[:3]:
            st.log(f"Role confirmed by headline: “{c['title']}” — {c['source']}", "ok")
        st.data(departure_signals=len(departures), confirming_headlines=len(confirms), cooccurring_articles=cooccur, successor=successor["name"] if successor else None)
        changed = bool(departures) and (successor is not None or len(departures) >= 2)
        if changed:
            sugg = f"{successor['name']} (per “{successor['item']['title']}”)" if successor else "the current holder of the role"
            st.status("fail", f"{prospect['name']} appears to have left {company}" + (f"; {successor['name']} named as successor" if successor else ""))
            evidence = [{"title": d["title"], "source": d["source"], "url": d["url"], "date": str(d.get("published"))[:10]} for d in departures[:4]]
            if successor:
                evidence.append({"title": successor["item"]["title"], "source": successor["item"]["source"], "url": successor["item"]["url"], "date": str(successor["item"].get("published"))[:10]})
            return _stop("NEEDS_RESEARCH", f"Public news indicates {prospect['name']} is no longer {prospect['title']} at {company}" + (f"; {successor['name']} was named to the role." if successor else ".") + " Draft withheld — the contact record is stale.",
                         **base, persona=persona, identity=ident, role_check={"status": "changed", "evidence": evidence, "successor": successor["name"] if successor else None}, research=R.stats,
                         next_steps=[f"Update the CRM record for {prospect['name']} (left {company}).", f"Re-target: research {sugg}.", f"Optional: {prospect['name']}'s new role may itself be a warm opening — research them separately."])
        if confirms:
            st.note(f"Role confirmed by {len(confirms)} headline(s)")
        elif cooccur:
            st.note(f"Name co-occurs with {company} in {cooccur} recent article(s); no departure news")
        else:
            st.status("warn", "Could not independently confirm the role (no public coverage) — rep should verify on LinkedIn")
        role_status = "confirmed" if confirms else ("likely" if cooccur else "unverified")

    # ------------------------------------------------------------------ 5. score
    with run.stage("score", "Classify & score signals") as st:
        items = []
        for it in news:
            c = S.classify_news(it, company, ident["aliases"], prospect["name"])
            items.append({**it, **c, "kind": "news", "credibility": S.credibility(it["source"], it.get("source_url", ""), ident["domain"])})
        clusters = S.cluster(items, company)
        sigs = []
        for cl in clusters:
            # most credible outlet first; among equals, the most specific headline (numbers, rounds), then the newest
            rep = sorted(cl, key=lambda x: (-x["credibility"], -len(re.findall(r"\d", x["title"])) - (3 if re.search(r"series [a-h]|earnings call|guidance|outlook", x["title"], re.I) else 0),
                                            x.get("days") if x.get("days") is not None else 999))[0]
            sigs.append({**rep, "text": rep["title"], "corroboration": len({x["source"] for x in cl}), "also": [{"title": x["title"], "source": x["source"], "url": x["url"]} for x in cl if x is not rep][:5],
                         "flags": sorted(set(sum((x["flags"] for x in cl), []))) if rep["type"] in ("sensitive", "personal") else rep["flags"]})
        job, jstats = S.job_signal(boards, company, as_of)
        if job:
            sigs.append(job)
            st.log(f"Hiring signal: {len(job['job_titles'])} back-office pain role(s) open — {', '.join(job['job_titles'][:4])}", "ok")
        elif boards:
            st.log(f"Job boards checked: {jstats['open_roles']} open roles, none in AP / procurement / reconciliation")
        if ident.get("site_description") or ident.get("description"):
            sigs.append({"type": "company_profile", "title": (ident.get("site_title") or company)[:120], "text": f"{ident.get('description', '')}. {ident.get('site_description', '')}",
                         "source": "Company website" if ident.get("site_description") else "Wikipedia", "url": f"https://{ident['domain']}" if ident.get("domain") else ident.get("wiki_url"),
                         "source_url": "", "published": None, "days": None, "kind": "profile", "flags": [], "credibility": 1.0, "corroboration": 1, "mentions": True, "subject": True, "names_person": False})
        for s in sigs:
            S.score(s, persona)
        sigs.sort(key=lambda s: (-s["score"], s.get("days") or 0))
        for i, s in enumerate(sigs, 1):
            s["id"] = f"S{i}"
        counts = {}
        for s in sigs:
            counts[s["type"]] = counts.get(s["type"], 0) + 1
        st.log(f"{len(items)} headlines → {len(clusters)} distinct events (same story from several outlets merged)")
        st.log("Types: " + ", ".join(f"{S.TYPE_LABEL[k]} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
        excluded = [s for s in sigs if s["type"] in ("personal", "sensitive")]
        for s in excluded[:5]:
            st.log(f"Excluded from hooks — {S.TYPE_LABEL[s['type']]}: “{s['title']}”", "warn")
        sensitive_recent = [s for s in sigs if s["type"] == "sensitive" and (s.get("days") or 999) <= 60 and (s["credibility"] >= 0.8 or s["corroboration"] >= 2)]
        st.data(rubric="score = 30% recency + 35% relevance to offer + 20% role fit + 15% source credibility (x0.75 if negative/rumour)",
                top=[f"{s['id']} {s['score']} {S.TYPE_LABEL[s['type']]}: {s['title'][:80]}" for s in sigs[:6]])
        if sensitive_recent:
            st.status("warn", f"Sensitive recent event: “{sensitive_recent[0]['title']}” — excluded and flagged")

    # ------------------------------------------------------------------ 6. hook
    with run.stage("hook", "Pick the hook") as st:
        eligible = [s for s in sigs if s["eligible"]]
        hook = eligible[0] if eligible and eligible[0]["score"] >= 45 else None
        secondary = None
        if hook:
            # a supporting signal must be a different kind of event and genuinely relevant (no sponsorship trivia)
            pool = [s for s in eligible[1:] if s["type"] != hook["type"] and s["score"] >= 60 and s["parts"]["relevance"] >= 0.6 and s["parts"]["role_fit"] >= 0.6]
            secondary = next((s for s in pool if s["type"] == "hiring_for_pain"), None) or (pool[0] if pool else None)
        top_any = sigs[0] if sigs else None
        why = []
        if hook:
            p = hook["parts"]
            why.append(f"{hook['id']} “{hook['title'][:90]}” scores {hook['score']}: recency {p['recency']}, relevance {p['relevance']}, role fit {p['role_fit']}, credibility {p['credibility']}"
                       + (f", corroborated by {hook['corroboration']} outlets" if hook.get("corroboration", 1) > 1 and hook.get("kind") == "news" else ""))
            if top_any and top_any is not hook and top_any["score"] > hook["score"]:
                why.append(f"Higher-scoring {top_any['id']} (“{top_any['title'][:70]}”) was passed over: {top_any['why_not']}")
            runner = next((s for s in eligible[1:] if s is not secondary), None)
            if runner:
                why.append(f"Beat {runner['id']} ({S.TYPE_LABEL[runner['type']]}, {runner['score']}) on " + (", ".join(k for k in ("recency", "relevance", "role_fit", "credibility") if hook["parts"][k] > runner["parts"][k]) or "overall score"))
            if secondary:
                why.append(f"Supporting signal {secondary['id']} ({S.TYPE_LABEL[secondary['type']]}) adds a second, different reason to talk")
            for w_ in why:
                st.log(w_, "ok" if w_ is why[0] else "info")
            st.note(f"Hook: {S.TYPE_LABEL[hook['type']]} — {hook['title'][:100]}")
        else:
            best = eligible[0] if eligible else None
            st.log("No company-specific signal is strong enough to build an email on" + (f" (best usable: {best['id']} scored {best['score']} < 45)" if best else " (no usable signals at all)"), "warn")
            st.log("Falling back to a role-level message: honest, generic by design, marked LOW_CONFIDENCE")
            st.status("warn", "No strong signal — role-level draft")
        tact = bool(sensitive_recent)
        if tact:
            st.log("Tact mode ON: copy will avoid any headcount / efficiency framing and won't reference the sensitive event", "warn")
        st.data(hook=hook["id"] if hook else None, secondary=secondary["id"] if secondary else None, tact_mode=tact)

    # ------------------------------------------------------------------ 7. draft
    with run.stage("draft", "Draft the message") as st:
        engine = "rules"
        draft = None
        rationale = " ".join(why) if hook else "No usable company-specific signal; role-level copy."
        if llm.available():
            try:
                st.log(f"Asking {llm.engine_name()} to choose the hook and write the draft (structured JSON)…")
                d = drafting.compose_llm(llm, prospect=prospect, persona=persona, company=company, seller=seller, ranked=sigs, tact=tact, rep_context=rep_context)
                if hook and not d["hook"]:
                    raise ValueError("model returned no valid hook id")
                if d["hook"] and d["hook"] is not hook:
                    st.log(f"Claude preferred {d['hook']['id']} over the rubric's {hook['id'] if hook else 'none'}: {d['rationale']}", "info")
                hook, secondary, draft = d["hook"] or hook, d["secondary"], d
                rationale = d["rationale"] or rationale
                engine = "claude"
            except Exception as e:
                st.log(f"Claude unavailable or invalid output ({type(e).__name__}: {str(e)[:120]}) — falling back to the rules engine", "warn")
        else:
            st.log("No API key configured — using the rules engine (signal-type × persona templates)")
        if draft is None:
            draft = drafting.compose_rules(prospect=prospect, persona=persona, company=company, seller=seller, hook=hook, secondary=secondary, tact=tact, rep_context=rep_context)
        st.log(f"Subject: {draft['subject']}")
        st.log(f"Draft written by {'Claude' if engine == 'claude' else 'rules engine'}: {drafting.word_count(draft['body'])} words, {len(draft['cites'])} cited source(s)", "ok")
        st.data(engine=engine, persona_angle=persona, citations=[f"[{i}] {c['source']}: {c['title'][:80]}" for i, c in enumerate(draft["cites"], 1)])

    # ------------------------------------------------------------------ 8. qa
    with run.stage("qa", "Quality & grounding check") as st:
        g = drafting.grounding_check(draft, company=company, prospect=prospect, seller=seller, rep_context=rep_context)
        for r in g["rows"]:
            if r["status"] == "supported":
                st.log(f"✓ cited claim supported by [{', '.join(map(str, r['cites']))}]: {r['sentence'][:110]}", "ok")
            elif r["status"] != "no factual claim":
                st.log(f"✕ {r['status']} — removed: “{r['sentence'][:110]}” (not found in source: {', '.join(r['missing'][:5])})", "error")
        body = g["body"]
        checks, pscore, pparts = drafting.quality_checks(subject=draft["subject"], body=body, linkedin=g["linkedin"], grounding=g, hook=hook, secondary=secondary, persona=persona, company=company)
        for c in checks:
            st.log(f"{c['name']}: {c['status'].upper()} — {c['detail']}", {"pass": "ok", "warn": "warn", "fail": "error"}.get(c["status"], "info"))
        st.data(personalisation_score=pscore, word_count=drafting.word_count(body), stripped_sentences=len(g["stripped"]))
        fails = [c for c in checks if c["status"] == "fail"]
        if fails:
            st.status("warn", f"{len(fails)} check(s) failed — rep must fix before sending")
        elif g["stripped"]:
            st.status("warn", f"{len(g['stripped'])} unsupported sentence(s) removed")

    # ------------------------------------------------------------------ 9. review
    with run.stage("review", "Queue for human review") as st:
        flags = []
        if tact:
            flags.append(f"Sensitive context: “{sensitive_recent[0]['title']}” ({sensitive_recent[0]['source']}). The draft avoids it — the rep should judge timing.")
        if role_status == "unverified":
            flags.append("Role not confirmed by any public source — check LinkedIn before sending.")
        if ident.get("domain_conflict") or ident.get("domain_suspect"):
            flags.append("Company domain could not be cleanly confirmed.")
        if R.stats["fallback"]:
            flags.append(f"{R.stats['fallback']} source(s) served from cache (live fetch failed) — facts may be slightly stale.")
        if fails:
            flags.append("Quality checks failed: " + "; ".join(c["name"] for c in fails))
        if not hook:
            decision = "LOW_CONFIDENCE"
        elif tact:
            decision = "REVIEW_SENSITIVE"
        elif fails or len(g["stripped"]) > 1:
            decision = "LOW_CONFIDENCE"
        else:
            decision = "READY_FOR_REVIEW"
        citations = [{"n": i, "id": c["id"], "type": c["type"], "type_label": S.TYPE_LABEL[c["type"]], "title": c["title"], "text": c.get("text") or c["title"], "source": c["source"], "url": c["url"],
                      "date": (c.get("published") or "")[:10] or ("open posting" if c.get("kind") == "job" else "")} for i, c in enumerate(draft["cites"], 1)]
        missing_info = []
        if decision == "LOW_CONFIDENCE":
            if not news:
                missing_info.append("No news coverage naming the company in the last 12 months")
            if not boards:
                missing_info.append("No public job board (Greenhouse / Lever / Ashby) to read hiring signals from")
            if not ident.get("domain"):
                missing_info.append("No company website / domain — add one to read how they describe themselves")
            if not ident.get("wiki_url"):
                missing_info.append("No Wikipedia entry to confirm basics (size, industry)")
            missing_info.append("Manual: check the prospect's recent LinkedIn posts or a podcast/interview for a personal hook")
        files = write_draft_files(run.id, prospect, company, draft["subject"], body, g["linkedin"], citations, decision, flags)
        st.log(f"Draft saved for the rep: {files['md']} and {files['eml']} (marked unsent)", "ok")
        st.log("Nothing has been sent. A rep must approve or edit the draft.", "ok")
        st.data(decision=decision, flags=len(flags))
        if decision == "READY_FOR_REVIEW":
            summary = f"Draft ready: {S.TYPE_LABEL[hook['type']].lower()} hook" + (f" + {S.TYPE_LABEL[secondary['type']].lower()}" if secondary else "") + f"; personalisation {pscore}/100."
        elif decision == "REVIEW_SENSITIVE":
            summary = f"Draft ready, sensitive context: avoided “{sensitive_recent[0]['title'][:60]}…”; hook: {S.TYPE_LABEL[hook['type']].lower()}."
        elif not hook:
            summary = "Only role-level personalisation possible — no strong public signal. Rep should add research."
        else:
            summary = "Draft needs work — quality checks failed or claims were removed."

    ranked = [{"id": s["id"], "type": s["type"], "type_label": S.TYPE_LABEL[s["type"]], "title": s["title"], "source": s["source"], "url": s["url"],
               "date": (s.get("published") or "")[:10] or ("open" if s.get("kind") == "job" else "—"), "days": round(s["days"]) if s.get("days") is not None else None,
               "score": s["score"], "parts": s["parts"], "eligible": s["eligible"], "why_not": s["why_not"], "flags": s["flags"],
               "corroboration": s.get("corroboration", 1), "also": s.get("also", []),
               "role": "hook" if hook is not None and s is hook else ("support" if secondary is not None and s is secondary else "")} for s in sigs[:20]]
    return {
        **base, "decision": decision, "summary": summary, "engine": "Claude" if engine == "claude" else "Rules engine",
        "persona": persona, "persona_label": S.PERSONAS[persona], "identity": {k: v for k, v in ident.items() if k != "extract"},
        "role_check": {"status": role_status}, "hook_rationale": rationale, "tact_mode": tact,
        "signals": ranked, "signal_count": len(sigs), "strong_signal": bool(hook and hook["score"] >= 60),
        "draft": {"subject": draft["subject"], "body": body, "linkedin": g["linkedin"], "citations": citations, "original_body": draft["body"] if g["stripped"] else None},
        "grounding": g["rows"], "checks": checks, "personalisation": pscore, "personalisation_parts": pparts,
        "flags": flags, "missing_info": missing_info, "files": files, "research": R.stats, "review": {"status": "pending"},
        "next_steps": ["Review the draft and the cited sources, edit if needed, then approve. Sending happens outside this tool."],
    }


# =============================================================================== outputs
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def write_draft_files(run_id: str, prospect: dict, company: str, subject: str, body: str, linkedin: str, citations: list[dict], decision: str, flags: list[str], review: dict | None = None) -> dict:
    DRAFTS.mkdir(exist_ok=True)
    stem = f"{_slug(prospect['name'])}_{_slug(company)}_{run_id}"
    status = (review or {}).get("status", "pending review")
    md = [f"# Outreach draft — {prospect['name']}, {prospect['title']} @ {company}", "",
          f"- **Status:** {decision} · review: {status}", "- **Never auto-sent.** Approve, edit, or discard.", ""]
    md += [f"> ⚠ {f}" for f in flags] + ([""] if flags else [])
    md += [f"**Subject:** {subject}", "", body, "", "---", "**LinkedIn note:** " + linkedin, "", "## Sources", ""]
    md += [f"[{c['n']}] {c['title']} — {c['source']}, {c['date']} — {c['url']}" for c in citations] or ["(none — role-level draft)"]
    (DRAFTS / f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    clean = re.sub(r"\s*\[\d+\]", "", body)
    eml = "\n".join([f"Subject: {subject}", f"To: \"{prospect['name']}\" <address-to-be-verified@invalid>", "X-Unsent: 1",
                     f"X-Outreach-Status: {decision} / {status}", "Content-Type: text/plain; charset=utf-8", "", clean, ""])
    (DRAFTS / f"{stem}.eml").write_text(eml, encoding="utf-8")
    return {"md": f"drafts/{stem}.md", "eml": f"drafts/{stem}.eml"}
