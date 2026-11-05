"""Turn raw research into classified, de-duplicated, scored signals.

A *signal* is one fact about the company that could open a conversation:
a funding round, an acquisition, results, a new exec, a job posting that
reveals a back-office pain, etc. Each gets:

  type         what kind of event it is (rules below, first match wins)
  flags        sensitive / personal / negative tone / rumour / mention-only
  score 0-100  = 30% recency + 35% relevance to what we sell
               + 20% fit with this person's role + 15% source credibility
  eligible     whether it may be used as the opening hook at all

The rubric is deliberately simple and explainable: a sales manager can read
the ranked table and see why the winner won.
"""
from __future__ import annotations

import re
from datetime import datetime

from research import age_days, domain_of

# ---------------------------------------------------------------- persona
PERSONAS = {
    "cfo": "Finance executive (CFO / finance chief)",
    "finance_ops": "Finance operations leader (Controller / AP / Procurement)",
    "ceo": "Company leader (CEO / founder / president / COO)",
    "other": "Other role",
}


def persona_of(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"\bcfo\b|chief financial|finance chief|chief finance", t):
        return "cfo"
    if re.search(r"controller|comptroller|accounts payable|\bap\b|payables|procure|purchasing|accounting|finance op|financial op|vp,? finance|vice president,? finance|head of finance|director of finance|finance director|treasur|shared services", t):
        return "finance_ops"
    if re.search(r"\bceo\b|chief executive|founder|president|\bcoo\b|chief operating|managing director|owner", t):
        return "ceo"
    return "other"


# ---------------------------------------------------------------- classification
R = lambda p: re.compile(p, re.I)  # noqa: E731

PERSONAL = R(r"\b(cfo|ceo|coo|cto|director|officer|executive|exec|insider|chair\w*|founder|president|trusts?|evp|svp)\b[^?]{0,80}\b(sells?|sold|buys|bought|acquires)\b[^?]{0,80}\b(stock|shares)\b|form 4|10b5-1|insider|trusts?\b.*\b(sell|sold)|shares? (award|withheld)|award .* vests|signing bonus|pay package|paid top executives|compensation|separation agreement|acquires shares")
SENSITIVE = R(r"layoffs?|lay(s|ing)? off|job cuts|cut(s|ting)? (about )?[\d,.]+%? ?(of )?(its )?(jobs|workforce|staff|employees|roles)|cutting .*workforce|\bcut\w* (its |the )?(global )?(workforce|staff|jobs|headcount)|workforce reduction|redundanc|downsiz|restructuring|furlough|lawsuit|\bsued\b|\bsues\b|class action|\bsettle(s|d)? (a |the )?(lawsuit|suit|claims?|case|charges|probe)|settlement|investigation|\bprobe\b|subpoena|indict|fraud|data breach|\bbreach\b|hack(ed|ers)?|ransomware|cyberattack|recall|bankrupt|chapter 11|insolven|\bstrikes?\b(?! deal)|scandal|upheaval|resigns amid|ousted|fined|penalty")
NEGATIVE = R(r"\b(slows?|slowing|slowdown|dent|miss(es|ed)?|falls?|fell|drops?|plunge|plummet|slump|tumble|sinks?|down \d+%|warns?|downgrade|loses|lost|rejects?|rejected|walk(s|ing|ed)? away|called off|terminat|scrap|delay|headwind|selloff|sell-off|concern|struggl|decline|dives?|underwhelm\w*|weigh(s|ing|ed)? on|flat\b|disappoint\w*|cuts? (its )?(outlook|guidance|forecast))")
RUMOUR = R(r"\breported(ly)?\b|\breport\b|\brumou?r|said to be|in talks|considering|weighs|\?|sources say|could\b")
STOCK = R(r"\(\w{2,6}\):|\((nasdaq|nyse)[:\s]|i still need|positioned to|should you|buy, sell|price target|\bstock\b|\bshares\b|valuation has|time to sell|load up|buy rating|sell rating|upgrade|52-week|analyst questions|\bsoars?\b|\bjump(s|ed)?\b|\bpops?\b|rall(y|ies)|top \d+ analyst|ticker")

PRICE_MOVE = R(r"\b(sinks?|falls?|slides?|drops?|jumps?|soars?|gains?|rises?|climbs?|tumbles?|plunges?|surges?|rallies|dips?)\s+(nearly |about |over )?\d+(\.\d+)?%")

TYPES = [
    ("exec_hire", R(r"\b(names?|appoints?|taps|hires?|promot\w+|welcomes|elects?)\b.*\b(cfo|chief|officer|president|controller|head of|vice president|evp|svp)\b|\bjoins\b.*\bas\b.*\b(cfo|chief|officer|president|head)\b|new (cfo|ceo|chief financial|finance chief)")),
    ("funding", R(r"\braises?\b.*\$|\braised \$|\braises? (a |its )?(new )?(round|series|seed)|series [a-h]\b|funding round|seed round|\bfunding\b|valuation of|at \$[\d.]+ ?(b|bn|billion) valuation|growth equity|closes \$")),
    ("m_and_a", R(r"\bacquir\w*|\bacquisition\b|\bbuys\b|\bto buy\b|\bmerger\b|\bmerge\b|takeover|\bdeal to\b")),
    ("earnings", R(r"\bearnings\b|\bq[1-4]\b|\bquarter\b|results|revenue|guidance|outlook|forecast|\bprofits?\b|fiscal")),
    ("product_launch", R(r"\bpartnership\b|\bpartners with\b|\bsponsor")),
    ("expansion", R(r"\bexpand\w*|expansion|opens? (new|its|a)|new (office|hub|headquarters|market|facility)|enters?\b|launch\w* in\b|international")),
    ("product_launch", R(r"\blaunch\w*|unveil\w*|introduc\w*|\badds?\b|rolls? out|debuts?|releases?|announces? new|partners? with|partnership")),
    ("ai_strategy", R(r"\b(launch\w*|deploy\w*|adopt\w*|invest\w*|rolls? out|bets? on|embrac\w*|partners?)\b.*\b(ai|agents?|agentic|automation)\b|automat\w+|efficien\w*|transformation|margins?|cost discipline")),
]

TYPE_LABEL = {
    "exec_hire": "Leadership change",
    "funding": "Funding",
    "m_and_a": "M&A",
    "earnings": "Results / guidance",
    "expansion": "Expansion",
    "product_launch": "Product / partnership",
    "ai_strategy": "Strategy / AI / efficiency",
    "hiring_for_pain": "Hiring for back-office pain",
    "company_profile": "Company profile",
    "stock_commentary": "Stock commentary",
    "sensitive": "Sensitive (layoffs / legal / breach)",
    "personal": "Personal (stock sales / pay)",
    "other": "General news",
}

# How useful each type is as a door-opener for a back-office automation seller.
RELEVANCE = {
    "hiring_for_pain": 1.0, "exec_hire": 0.8, "m_and_a": 0.8, "ai_strategy": 0.75, "funding": 0.75,
    "earnings": 0.7, "expansion": 0.65, "product_launch": 0.35, "other": 0.3, "company_profile": 0.2,
    "stock_commentary": 0.15, "sensitive": 0.5, "personal": 0.0,
}
# How much each type matters to each persona.
ROLE_FIT = {
    "funding":         {"cfo": 0.8, "finance_ops": 0.5, "ceo": 1.0, "other": 0.5},
    "m_and_a":         {"cfo": 0.9, "finance_ops": 0.7, "ceo": 0.9, "other": 0.5},
    "earnings":        {"cfo": 1.0, "finance_ops": 0.5, "ceo": 0.7, "other": 0.4},
    "exec_hire":       {"cfo": 0.9, "finance_ops": 0.8, "ceo": 0.6, "other": 0.4},
    "hiring_for_pain": {"cfo": 0.8, "finance_ops": 1.0, "ceo": 0.5, "other": 0.5},
    "expansion":       {"cfo": 0.7, "finance_ops": 0.6, "ceo": 0.9, "other": 0.5},
    "product_launch":  {"cfo": 0.4, "finance_ops": 0.3, "ceo": 0.8, "other": 0.5},
    "ai_strategy":     {"cfo": 0.9, "finance_ops": 0.7, "ceo": 0.8, "other": 0.5},
}
FINANCE_WORDS = R(r"\b(cfo|finance|financial|accounts payable|invoice|procure|spend|expense|cost|margin|back[- ]office|automation|close|audit|vendor|supplier)\b")

# Source credibility: the company itself / official APIs > tier-1 press > trade press > aggregators.
TIER1 = R(r"reuters|wall street journal|\bwsj\b|bloomberg|financial times|\bft\b|cnbc|associated press|\bap news\b|axios|new york times|the information|fortune|forbes|business wire|pr newswire|globe ?newswire|techcrunch|newsroom|investor relations|calcalist|ctech|globes")
TIER2 = R(r"dive|freightwaves|business insider|businessinsider|yahoo|barron|marketwatch|venturebeat|the verge|wired|siliconangle|fierce|insurance business|crain|business journals|commercial appeal|human resources director|quartz|fast company|benzinga|pymnts|cfo\.com|cfo brew|investing\.com")
AGGREGATOR = R(r"tikr|stock titan|gurufocus|kalkine|biggo|tradingview|simply wall|marketscreener|quiver|pluang|indexbox|n24|fourweekmba|tipranks|marketbeat|zacks|seeking alpha|stockstory|globe and mail|investing\.com (india|uk|canada|nigeria|south africa)")


def credibility(source: str, source_url: str, company_domain: str = "") -> float:
    s = f"{source} {source_url}".lower()
    if company_domain and company_domain in domain_of(source_url):
        return 1.0
    if AGGREGATOR.search(s):
        return 0.5
    if TIER1.search(s):
        return 0.95
    if TIER2.search(s):
        return 0.8
    return 0.65


def recency_score(days: float | None, kind: str = "news") -> float:
    if days is None:
        return 0.3
    if kind == "job":  # an open posting is a *current* fact; staleness only matters if it is very old
        return 0.9 if days <= 120 else 0.6
    for limit, val in ((14, 1.0), (45, 0.85), (90, 0.65), (180, 0.4), (365, 0.2)):
        if days <= limit:
            return val
    return 0.1


def _name_rx(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.I)


def is_about(title: str, company: str, aliases: list[str]) -> tuple[bool, bool]:
    """(mentions_company, company_is_the_subject)."""
    names = [company] + aliases
    hit = None
    for n in names:
        m = _name_rx(n).search(title)
        if m and (hit is None or m.start() < hit.start()):
            hit = m
    if not hit:
        return False, False
    head = title[: hit.start()]
    # subject if within first ~6 words, or after a colon like "Exclusive: Upwind buys ..."
    words_before = len(re.findall(r"\w+", head.split(":")[-1]))
    return True, words_before <= 3


def classify_news(item: dict, company: str, aliases: list[str], person: str) -> dict:
    title = item["title"]
    flags = []
    if PERSONAL.search(title):
        typ = "personal"
    elif PRICE_MOVE.search(title):
        typ = "stock_commentary"  # "X sinks 12% despite <someone else's> hack" is market noise, not an event at X
    elif SENSITIVE.search(title):
        typ = "sensitive"
    else:
        typ = "other"
        for t, rx in TYPES:
            if rx.search(title):
                typ = t
                break
        if typ in ("other", "ai_strategy", "earnings") and STOCK.search(title) and not re.search(r"earnings call|results|outlook|guidance", title, re.I):
            typ = "stock_commentary"
    if typ == "m_and_a":
        names = "|".join(re.escape(n) for n in [company] + aliases)
        acquirer = re.search(rf"(?:{names})(?:'s|’s)?\s+(?:\w+\s+){{0,2}}(?:acquires|acquired|acquiring|buys|bought|to acquire|to buy|completes|closes|agrees|snaps up|picks up)", title, re.I)
        target = re.search(rf"(?:acquisition of|acquires|buys|to acquire|to buy)\s+(?:\w+\s+){{0,3}}(?:{names})\b", title, re.I)
        if not acquirer and not target:
            typ = "other"
    if NEGATIVE.search(title):
        flags.append("negative tone")
    if RUMOUR.search(title):
        flags.append("unconfirmed / rumour")
    mentions, subject = is_about(title, company, aliases)
    if mentions and not subject:
        flags.append("company only mentioned")
    last = (person or "").split()[-1] if person else ""
    names_person = bool(last and len(last) > 2 and _name_rx(last).search(title))
    return {"type": typ, "flags": flags, "mentions": mentions, "subject": subject, "names_person": names_person}


def _tokens(s: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "after", "into", "its", "that", "this", "what", "here", "says", "said", "will", "more", "than", "over", "about", "as", "at", "to", "of", "in", "on", "a", "an", "is", "are", "by"}
    return {w for w in re.findall(r"[a-z0-9$%.]+", s.lower()) if len(w) > 2 and w not in stop}


def _nums(s: str) -> set[str]:
    return {re.sub(r"[^0-9.]", "", m) for m in re.findall(r"\$?\d[\d,.]*\s?(?:%|m|b|bn|million|billion)?", s.lower()) if len(re.sub(r"[^0-9]", "", m)) >= 2}


def close7(a: dict, b: dict) -> bool:
    return a.get("days") is not None and b.get("days") is not None and abs(a["days"] - b["days"]) <= 7


def cluster(items: list[dict], company: str) -> list[list[dict]]:
    """Group headlines that describe the same event (e.g. 30 outlets covering one CFO appointment)."""
    ctoks = _tokens(company)
    clusters: list[list[dict]] = []
    for it in items:
        toks = _tokens(it["title"]) - ctoks
        nums = _nums(it["title"])
        placed = False
        for cl in clusters:
            head = cl[0]
            if head["type"] != it["type"]:
                continue
            htoks = _tokens(head["title"]) - ctoks
            jac = len(toks & htoks) / max(1, len(toks | htoks))
            same_num = bool(nums & _nums(head["title"]))
            close = head.get("days") is not None and it.get("days") is not None and abs(head["days"] - it["days"]) <= 3
            hq, iq = re.findall(r"\bq[1-4]\b", head["title"].lower()), re.findall(r"\bq[1-4]\b", it["title"].lower())
            same_q = it["type"] == "earnings" and hq and iq and hq[0] == iq[0] and (head.get("days") is None or it.get("days") is None or abs(head["days"] - it["days"]) <= 21)
            same_q = same_q or (it["type"] == "earnings" and close7(head, it))
            if same_q or jac >= 0.3 or (same_num and it["type"] not in ("other", "stock_commentary")) or (close and it["type"] in ("exec_hire", "funding", "m_and_a", "sensitive", "earnings") and jac >= 0.12):
                cl.append(it)
                placed = True
                break
        if not placed:
            clusters.append([it])
    return clusters


# ---------------------------------------------------------------- job postings
CORE_PAIN = R(r"accounts payable|\bpayables?\b|\bAP\b|procure[- ]to[- ]pay|\bP2P\b|purchase to pay|vendor (master|onboarding|management|operations)|supplier (onboarding|operations)|reconciliation|\bprocurement\b|invoice|expense")
ADJ_PAIN = R(r"accounting|accountant|controller|general ledger|\bGL\b|revenue accounting|order to cash|record to report|payroll|billing operations|financial operations|finance operations|\btax\b|treasury|financial systems|finance systems")
EXCLUDE_JOB = R(r"sales|account executive|engineer|developer|intern|marketing|designer|legal counsel|recruit")


def job_signal(boards: list[dict], company: str, as_of: datetime) -> tuple[dict | None, dict]:
    """Summarise open roles across boards; return (signal or None, stats)."""
    core, adj, total = [], [], 0
    for b in boards:
        for j in b["jobs"]:
            total += 1
            t = j["title"]
            if EXCLUDE_JOB.search(t) and not re.search(r"accounts payable|procure", t, re.I):
                continue
            if CORE_PAIN.search(t):
                core.append({**j, "provider": b["provider"]})
            elif ADJ_PAIN.search(t):
                adj.append({**j, "provider": b["provider"]})
    stats = {"open_roles": total, "core_pain_roles": len(core), "adjacent_finance_roles": len(adj)}
    if not core:
        return None, stats
    # prefer AP-specific titles first
    core.sort(key=lambda j: (0 if re.search(r"accounts payable|payables", j["title"], re.I) else 1, j["title"]))
    titles = []
    for j in core:
        if j["title"] not in titles:
            titles.append(j["title"])
    newest = min((age_days(j.get("updated"), as_of) or 999) for j in core)
    text = f"{company} open roles: " + "; ".join(titles[:6])
    if adj:
        text += f" (plus {len(adj)} other accounting/finance-ops roles)"
    sig = {
        "type": "hiring_for_pain",
        "title": f"Hiring: {', '.join(titles[:3])}" + (f" (+{len(titles) - 3} more)" if len(titles) > 3 else ""),
        "text": text,
        "job_titles": titles,
        "source": f"{core[0]['provider'].title()} job board",
        "source_url": core[0]["url"],
        "url": core[0]["url"],
        "published": None,
        "days": newest if newest < 999 else None,
        "kind": "job",
        "flags": [],
        "corroboration": len(core),
        "credibility": 1.0,
        "mentions": True,
        "subject": True,
        "names_person": False,
    }
    return sig, stats


# ---------------------------------------------------------------- scoring
INELIGIBLE_TYPES = {"personal", "sensitive", "stock_commentary", "company_profile", "other"}


def score(sig: dict, persona: str) -> dict:
    typ = sig["type"]
    rec = recency_score(sig.get("days"), sig.get("kind", "news"))
    rel = RELEVANCE.get(typ, 0.3)
    if FINANCE_WORDS.search(sig["title"]) and typ not in ("personal", "sensitive"):
        rel = min(1.0, rel + 0.15)
    if "company only mentioned" in sig["flags"]:
        rel *= 0.4
    fit = ROLE_FIT.get(typ, {}).get(persona, 0.3)
    if sig.get("names_person"):
        fit = min(1.0, fit + 0.2)
    cred = sig.get("credibility", 0.65)
    if sig.get("corroboration", 1) >= 3:
        cred = min(1.0, cred + 0.1)
    total = 100 * (0.30 * rec + 0.35 * rel + 0.20 * fit + 0.15 * cred)
    if "negative tone" in sig["flags"] or "unconfirmed / rumour" in sig["flags"]:
        total *= 0.75
    reasons = []
    eligible = True
    if typ in INELIGIBLE_TYPES:
        eligible = False
        reasons.append({"personal": "personal / insider-trading item — never used in outreach",
                        "sensitive": "sensitive topic — never used as an opening hook",
                        "stock_commentary": "stock-price commentary — not a business event",
                        "company_profile": "background only, not a timely event",
                        "other": "no clear business event"}[typ])
    for f in ("negative tone", "unconfirmed / rumour", "company only mentioned"):
        if f in sig["flags"]:
            eligible = False
            reasons.append({"negative tone": "negative framing — would read as tone-deaf",
                            "unconfirmed / rumour": "unconfirmed / rumour — can't state it as fact",
                            "company only mentioned": "story is about another company"}[f])
    if sig.get("days") is not None and sig.get("kind") != "job" and sig["days"] > 270:
        eligible = False
        reasons.append("older than 9 months")
    sig.update(score=round(total), parts={"recency": round(rec, 2), "relevance": round(rel, 2), "role_fit": round(fit, 2), "credibility": round(cred, 2)},
               eligible=eligible, why_not="; ".join(reasons))
    return sig
