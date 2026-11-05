"""Compose the outreach draft, then check it.

Two engines produce the same structure (subject, body with [n] citation
markers, LinkedIn note):
  * rules  — signal-type x persona templates that lift concrete facts
             (amounts, rounds, job titles, names) out of the cited source.
  * Claude — when an API key is present: picks/explains the hook and writes
             the copy as structured JSON. Any failure falls back to rules.

Whichever engine wrote it, `grounding_check()` then verifies every factual
claim against the source it cites and strips sentences it can't support, and
`quality_checks()` scores the result. The engines are not trusted; the checks are.
"""
from __future__ import annotations

import re

from signals import PERSONAS, TYPE_LABEL

BANNED = [
    "i hope this email finds you well", "hope you're doing well", "hope you are well", "just checking in", "touch base",
    "circle back", "synergy", "synergies", "game-changer", "game changer", "revolutionary", "cutting-edge", "best-in-class",
    "world-class", "leverage", "quick question", "i came across your profile", "i wanted to reach out", "reaching out because",
    "to whom it may concern", "dear sir", "low-hanging fruit", "paradigm", "disrupt", "unlock value", "at the end of the day",
    "as per my last email", "guaranteed", "100%",
]
SENSITIVE_IN_DRAFT = re.compile(r"layoff|job cuts|workforce|redundan|restructur|lawsuit|\bsued\b|settlement|breach|investigation|stock sale|sold shares|insider|compensation|signing bonus|severance|replace (your|the) team|fewer people|cut (head ?count|staff|jobs)|headcount", re.I)
CLAIM_WORDS = re.compile(r"\b(raised|raise|raising|funding|series [a-h]|acquir\w*|picked up|bought|hiring|hired|named|appointed|launched|launching|adding|expanding|expanded|results|earnings|outlook|guidance|quarter|valuation|closed|closing)\b", re.I)

# --------------------------------------------------------------------------- persona copy
ANGLE = {
    "cfo": "{seller} puts AI agents on the repetitive parts of AP, vendor onboarding and reconciliations: they read the invoice, match it, chase the missing detail and hand your team only the exceptions, so the close moves faster and every step leaves an audit trail.",
    "finance_ops": "{seller}'s agents take invoice intake, PO matching and vendor follow-ups off the team's plate and route only the true exceptions to a person on your team.",
    "ceo": "{seller} deploys AI agents that run back-office finance work (AP, vendor onboarding, reconciliations), so finance keeps pace with new hires, vendors and entities instead of becoming the bottleneck.",
    "other": "{seller} deploys AI agents that handle back-office finance work like AP, vendor onboarding and reconciliations.",
}
ANGLE_TACTFUL = {
    "cfo": "{seller} puts AI agents on the repetitive parts of AP, vendor onboarding and reconciliations. They prepare the work, your team stays in control of every decision, and each step is logged for audit.",
    "finance_ops": "{seller}'s agents take invoice intake and PO matching off the team's plate, with every exception routed to a person to decide.",
    "ceo": "{seller} deploys AI agents for back-office finance work, with your team reviewing every exception.",
    "other": "{seller} deploys AI agents for back-office finance work, with people reviewing every exception.",
}
CTA = {
    "cfo": "Worth 20 minutes to see how finance teams at your scale are taking that work off the close calendar?",
    "finance_ops": "Open to comparing notes on how invoices get from inbox to approval at {company} today?",
    "ceo": "Would a one-page sketch of where agents fit in {company}'s back office be useful?",
    "other": "Would it be useful if I sent over a short overview?",
}
BRIDGE = {  # (type, persona) -> why this event matters to this person; hedged, never a claim about them
    ("funding", "ceo"): "Rounds like that usually come with a hiring plan, and the back office tends to be the last place anyone budgets for.",
    ("funding", "cfo"): "A new round usually means more vendors, more entities and an audit bar that rises faster than the finance team grows.",
    ("funding", "*"): "Growth on that scale usually shows up first as invoice and vendor volume.",
    ("m_and_a", "*"): "Deals like that tend to land on finance as two vendor masters, two approval flows and a longer close until systems converge.",
    ("earnings", "cfo"): "When growth is the headline, the quieter question is how to keep finance operations from scaling linearly with transaction volume.",
    ("earnings", "*"): "Strong quarters tend to bring more volume through finance operations.",
    ("exec_hire", "*"): "New finance leadership is often when the AP and close processes get a fresh look.",
    ("hiring_for_pain", "finance_ops"): "Openings like those are usually a sign that invoice volume has outgrown the current process.",
    ("hiring_for_pain", "*"): "Openings like those are usually a sign that invoice and vendor volume is growing faster than the process around it.",
    ("expansion", "*"): "New markets usually mean new entities, currencies and vendors for finance to absorb.",
    ("product_launch", "*"): "Launches like that tend to add vendors and spend lines faster than finance processes adapt.",
    ("ai_strategy", "*"): "If AI is on the agenda for the product, the back office is often the next place teams look for it.",
}
TACT_BRIDGE = "Not a pitch for right now, just a thought for when you're planning next year's finance operations."
ROLE_LEVEL_BRIDGE = {
    "cfo": "For most finance leaders, AP and vendor onboarding are where manual work quietly piles up as the business grows.",
    "finance_ops": "For most controllers and AP leads, the work that piles up is invoice intake, PO matching and chasing vendor details.",
    "ceo": "At most growing companies, AP and vendor onboarding are where manual work quietly piles up in the back office.",
    "other": "At most growing companies, AP and vendor onboarding are where manual work quietly piles up.",
}


def _first(name: str) -> str:
    return (name or "there").split()[0]


def _a(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _clean_company(company: str) -> str:
    return company.strip()


# --------------------------------------------------------------------------- fact extraction
AMOUNT = re.compile(r"\$\s?\d[\d.,]*\s?(?:million|billion|bn|m|b)\b", re.I)
ROUND = re.compile(r"\bSeries [A-H]\b", re.I)
QUARTER = re.compile(r"\bQ[1-4]\b|\b(?:first|second|third|fourth)[- ]quarter\b", re.I)


def _q(m) -> str:
    """'Q3' stays 'Q3'; 'Third-Quarter' becomes 'third-quarter' (mid-sentence)."""
    return m.group(0) if m.group(0)[:1] in "Qq" else m.group(0).lower()


def _fmt_amount(a: str) -> str:
    a = a.replace(" ", "")
    m = re.match(r"\$([\d.,]+)(million|billion|bn|m|b)", a, re.I)
    if not m:
        return a
    unit = m.group(2).lower()
    return f"${m.group(1)}{'M' if unit in ('m', 'million') else 'B'}"


def _target_of(title: str, company: str) -> str | None:
    verbs = r"\b(acquires|acquired|acquiring|buys|bought|to buy|to acquire|acquisition of)\b(.*)"
    m = re.search(re.escape(company) + r".*?" + verbs, title, re.I) or re.search(verbs, title, re.I)
    if not m:
        return None
    rest = re.split(r"\s(?:for|in|at|from|amid|as|to)\s|[,;:(]| - ", m.group(2))[0]
    caps = re.findall(r"\b[A-Z][\w&.\-]*(?:\s[A-Z][\w&.\-]*)*", rest)
    caps = [c for c in caps if c.lower() != company.lower() and c not in ("AI",)]
    return caps[-1] if caps else None


def _appointee(title: str) -> tuple[str | None, str | None]:
    m = re.search(r"\b(?:names|appoints|taps|hires|promotes|elects|welcomes)\s+(?:\w+\s+(?:executive|alum|veteran|exec)\s+)?([A-Z][a-z]+(?:\s[A-Z]\.)?(?:\s[A-Z][\w'\-]+)+)", title)
    if not m:  # passive form: "Jamere Jackson named next FedEx CFO"
        m = re.search(r"([A-Z][a-z]+(?:\s[A-Z]\.)?(?:\s[A-Z][\w'\-]+)+)\s+(?:is\s+)?(?:named|appointed|tapped|hired|promoted)\b", title)
    who = m.group(1) if m else None
    role = None
    for rx, lab in ((r"chief financial officer|\bcfo\b", "CFO"), (r"chief executive|\bceo\b", "CEO"), (r"chief operating|\bcoo\b", "COO"), (r"controller", "Controller")):
        if re.search(rx, title, re.I):
            role = lab
            break
    return who, role


def opener(sig: dict, n: int, company: str) -> str:
    """One grounded opening sentence for a signal, with its citation marker."""
    t, title = sig["type"], sig["title"]
    mk = f" [{n}]"
    if t == "funding":
        amt = AMOUNT.search(title)
        rnd = ROUND.search(title)
        parts = " ".join(x for x in [_fmt_amount(amt.group(0)) if amt else "", rnd.group(0).title().replace("Series", "Series") if rnd else ""] if x)
        return f"Congrats on the {parts} round{mk}." if parts else f"Congrats on the new funding round{mk}."
    if t == "m_and_a":
        tgt = _target_of(title, company)
        return f"Saw that {company} picked up {tgt}{mk}." if tgt else f"Saw the acquisition news from {company}{mk}."
    if t == "earnings":
        q = QUARTER.search(title)
        if re.search(r"rais\w* .*(outlook|guidance|forecast)|(outlook|guidance|forecast) (raised|lifted|boost)", title, re.I):
            what = "guidance" if re.search("guidance", title, re.I) else "outlook"
            return f"Saw that {company} raised its {what}" + (f" with the {_q(q)} results" if q else "") + f"{mk}."
        if re.search(r"\bcall\b", title, re.I):
            return f"I read the highlights from {company}'s {_q(q) + ' ' if q else ''}earnings call{mk}."
        return f"Saw {company}'s {_q(q) + ' ' if q else 'latest '}results{mk}."
    if t == "exec_hire":
        who, role = _appointee(title)
        if who and role:
            return f"Saw that {company} named {who} as {role}{mk}."
        return f"Saw the leadership news at {company}{mk}."
    if t == "hiring_for_pain":
        jt = sig.get("job_titles", [])[:2]
        if len(jt) == 2:
            return f"Noticed {company} is hiring for {jt[0]} and {jt[1]} roles{mk}."
        return f"Noticed {company} is hiring {_a(jt[0])} {jt[0]}{mk}."
    # product launches, expansion, strategy: quote the headline rather than paraphrase loosely
    src = sig.get("source") or "the"
    return f"I read the {src} piece, \"{title}\"{mk}."


def _bridge(sig_type: str, persona: str) -> str:
    return BRIDGE.get((sig_type, persona)) or BRIDGE.get((sig_type, "*")) or ROLE_LEVEL_BRIDGE[persona]


def _subject(hook: dict | None, company: str, persona: str) -> str:
    if not hook:
        return {"cfo": f"AP workload at {company}", "finance_ops": f"Invoice intake at {company}", "ceo": f"{company}'s back office"}.get(persona, f"Back-office work at {company}")
    t = hook["type"]
    if t == "funding":
        rnd = ROUND.search(hook["title"])
        return f"After the {rnd.group(0).title()}: {company}'s back office" if rnd else f"After the raise: {company}'s back office"
    if t == "m_and_a":
        tgt = _target_of(hook["title"], company)
        return f"Folding {tgt} into {company}'s finance ops" if tgt else f"{company}'s integration work"
    if t == "earnings":
        q = QUARTER.search(hook["title"])
        return f"After {company}'s {_q(q)} results" if q else f"{company}'s latest results"
    if t == "exec_hire":
        return f"New finance leadership at {company}"
    if t == "hiring_for_pain":
        return f"{company}'s AP hiring"
    return f"An idea for {company}'s finance team"


def compose_rules(*, prospect: dict, persona: str, company: str, seller: dict, hook: dict | None, secondary: dict | None, tact: bool, rep_context: str) -> dict:
    first = _first(prospect["name"])
    sname = seller["name"]
    lines = [f"Hi {first},", ""]
    cites = []
    if hook:
        cites.append(hook)
        p1 = opener(hook, 1, company) + " " + (TACT_BRIDGE if tact else _bridge(hook["type"], persona))
    else:
        p1 = ROLE_LEVEL_BRIDGE[persona]
        if rep_context:
            p1 = f"From what I understand of {company} ({rep_context.strip().rstrip('.')}), " + p1[0].lower() + p1[1:]
    lines.append(p1)
    if secondary:
        cites.append(secondary)
        n = len(cites)
        if secondary["type"] == "hiring_for_pain":
            jt = secondary.get("job_titles", [])[:2]
            what = f"{_a(jt[0])} {jt[0]}" if len(jt) < 2 else f"{jt[0]} and {jt[1]} roles"
            s2 = f"I also noticed the open {what} posting{'s' if len(jt) > 1 else ''} [{n}], which usually means volume is outgrowing the current process."
            if len(jt) >= 2:
                s2 = f"I also noticed you're hiring for {jt[0]} and {jt[1]} roles [{n}], which usually means volume is outgrowing the current process."
        elif secondary["type"] == "earnings" and QUARTER.search(secondary["title"]):
            s2 = f"And congratulations on the {_q(QUARTER.search(secondary['title']))} results [{n}]."
        else:
            o2 = opener(secondary, n, company)
            s2 = "Separately, I " + o2[0].lower() + o2[1:] if o2.startswith("Saw") else ("Separately, " + o2 if o2.startswith("I ") else "Separately: " + o2[0].lower() + o2[1:])
        lines += ["", s2]
    lines += ["", (ANGLE_TACTFUL if tact else ANGLE)[persona].format(seller=sname), "", CTA[persona].format(company=company), "", seller.get("rep_name") or "", sname]
    body = "\n".join(lines).strip()
    note_hook = ""
    if hook:
        note_hook = re.sub(r"\s*\[\d+\]", "", opener(hook, 1, company)).rstrip(".")
        note_hook = note_hook[0].lower() + note_hook[1:]
    linkedin = (f"Hi {first}, {note_hook}. " if hook and not tact else f"Hi {first}, ") + f"I work on {seller.get('short') or 'finance back-office automation'} at {sname} and would be glad to connect."
    if len(linkedin) > 300:
        linkedin = f"Hi {first}, I work on {seller.get('short') or 'finance back-office automation'} at {sname} and would be glad to connect."
    return {"subject": _subject(hook, company, persona), "body": body, "linkedin": linkedin, "cites": cites}


# --------------------------------------------------------------------------- LLM path
LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "hook_signal_id": {"type": "string"},
        "secondary_signal_id": {"type": "string"},
        "hook_rationale": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "linkedin_note": {"type": "string"},
    },
}


def compose_llm(llm, *, prospect, persona, company, seller, ranked, tact, rep_context) -> dict:
    eligible = [s for s in ranked if s["eligible"]][:8]
    sig_lines = "\n".join(
        f"- id={s['id']} type={s['type']} score={s['score']} date={(s.get('published') or 'open posting')[:10]} source={s['source']}\n  text: {s.get('text') or s['title']}" for s in eligible)
    system = (
        "You write first-touch B2B sales emails that a human rep will review before sending. "
        "Rules: (1) every fact about the prospect's company must come from the numbered signals provided and be followed by its marker like [S3] (use the signal id); "
        "(2) never invent numbers, names, products or events; (3) no flattery, no filler, no buzzwords; (4) 80-130 words in the body including greeting and sign-off; "
        "(5) one clear, low-friction question as the call to action; (6) never mention layoffs, lawsuits, stock sales, pay, or anything personal; "
        "(7) seller claims must stay within the seller description given."
    )
    prompt = (
        f"Prospect: {prospect['name']}, {prospect['title']} at {company}. Persona: {PERSONAS[persona]}.\n"
        f"Seller: {seller['name']} — {seller['one_liner']}. Rep signing the email: {seller.get('rep_name') or seller['name']}.\n"
        f"Rep notes (unverified): {rep_context or 'none'}\n"
        f"{'CAUTION: the company has a recent sensitive event (e.g. layoffs). Do not reference it, avoid any framing about reducing headcount, keep the tone low-key.' if tact else ''}\n"
        f"Eligible signals (ranked by our scoring rubric):\n{sig_lines or '(none — write a role-level email with no company-specific claims)'}\n\n"
        "Pick the single best hook (and optionally one supporting signal with a different type), explain the choice in one or two sentences "
        "for the rep, then write the subject (max 8 words), the email body, and a LinkedIn connection note under 280 characters. "
        "Return empty string for secondary_signal_id if you do not use one."
    )
    out = llm.complete_json(system, prompt, LLM_SCHEMA, effort="medium", max_tokens=4000)
    by_id = {s["id"]: s for s in ranked}
    hook = by_id.get(out.get("hook_signal_id"))
    sec = by_id.get(out.get("secondary_signal_id") or "")
    if hook and not hook["eligible"]:
        raise ValueError(f"model chose ineligible signal {hook['id']}")
    # renumber [S3] -> [1], [S7] -> [2] in order of first appearance
    order: list[str] = []
    body = out["body"]
    for sid in re.findall(r"\[(S\d+)\]", body):
        if sid not in order:
            order.append(sid)
    for i, sid in enumerate(order, 1):
        body = body.replace(f"[{sid}]", f"[{i}]")
    cites = [by_id[sid] for sid in order if sid in by_id]
    linked = re.sub(r"\s*\[S\d+\]", "", out.get("linkedin_note", ""))[:300]
    return {"subject": out["subject"], "body": body, "linkedin": linked, "cites": cites,
            "hook": hook, "secondary": sec, "rationale": out.get("hook_rationale", "")}


# --------------------------------------------------------------------------- grounding
def _norm_num(s: str) -> str:
    s = s.lower().replace(",", "").replace("$", "").replace(" ", "")
    s = s.replace("million", "m").replace("billion", "b").replace("bn", "b")
    return s


def _sentences(text: str) -> list[str]:
    out = []
    for para in text.split("\n"):
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", para.strip()) if para.strip() else [""]
        out.extend(parts)
        out.append("\n")
    return out[:-1] if out else out


ALLOW_CAPS = {"Hi", "I", "Congrats", "Saw", "Noticed", "Read", "Separately", "Worth", "Would", "Open", "Rounds", "Deals", "Strong", "New",
              "When", "For", "At", "If", "Openings", "Growth", "From", "AP", "PO", "AI", "CFO", "CEO", "COO", "No", "Folding", "After", "An",
              "The", "A", "It", "We", "Our", "You", "Your", "Happy", "Thanks", "Best", "Launches", "That", "This", "Most", "Controller", "Q1", "Q2", "Q3", "Q4"}


def claim_terms(sentence: str, allow: set[str]) -> list[str]:
    """The checkable bits of a sentence: numbers/amounts and proper-noun-like terms not on the allow-list."""
    s = re.sub(r"\[\d+\]", "", sentence)
    s = re.sub(r"\b\d+[- ]minutes?\b", "", s)  # meeting length in the CTA is not a claim
    terms = [m.group(0) for m in re.finditer(r"\$?\d[\d,.]*\s?(?:%|million|billion|bn|M|B)?", s) if re.search(r"\d", m.group(0))]
    first = re.search(r"[A-Za-z]", s)
    for m in re.finditer(r"\b[A-Z][\w&'\-]*", s):
        if first and m.start() == first.start():
            continue  # sentence-initial capital is grammar, not a proper noun
        w = re.sub(r"(?:'s|’s|'|’)$", "", m.group(0))
        if w in ALLOW_CAPS or w.lower() in allow:
            continue
        terms.append(w)
    return terms


def grounding_check(draft: dict, *, company: str, prospect: dict, seller: dict, rep_context: str) -> dict:
    """Verify each sentence's claims against the sources it cites; strip what can't be supported."""
    cites = draft["cites"]
    allow = {w.lower() for w in re.findall(r"[\w&'\-]+", f"{company} {prospect['name']} {seller['name']} {seller.get('rep_name', '')}")}
    seller_text = f"{seller['name']} {seller.get('one_liner', '')} {seller.get('short', '')}".lower()
    rows, kept, stripped = [], [], []
    for sent in _sentences(draft["body"]):
        if sent in ("\n", ""):
            kept.append(sent)
            continue
        marks = [int(x) for x in re.findall(r"\[(\d+)\]", sent)]
        terms = claim_terms(sent, allow)
        if marks:
            src = " ".join(f"{c['title']} {c.get('text', '')} {c.get('source', '')}" for i, c in enumerate(cites, 1) if i in marks)
            bad_marks = [m for m in marks if m < 1 or m > len(cites)]
            norm_src = _norm_num(src)
            missing = [t for t in terms if (_norm_num(t) not in norm_src if re.search(r"\d", t) else t.lower() not in src.lower())]
            ok = not missing and not bad_marks
            rows.append({"sentence": sent, "cites": marks, "status": "supported" if ok else "unsupported", "missing": missing + [f"[{m}] (no such source)" for m in bad_marks]})
        else:
            nums = [t for t in terms if re.search(r"\d", t) and _norm_num(t) not in _norm_num(seller_text + " " + rep_context)]
            names = [t for t in terms if not re.search(r"\d", t) and t.lower() not in seller_text and t.lower() not in rep_context.lower()]
            claims_event = bool(CLAIM_WORDS.search(sent)) and company.lower() in sent.lower()
            ok = not nums and not claims_event and not names
            rows.append({"sentence": sent, "cites": [], "status": "no factual claim" if ok else "uncited claim", "missing": nums + names + (["event claim without a citation"] if claims_event else [])})
        if rows[-1]["status"] in ("supported", "no factual claim"):
            kept.append(sent)
        else:
            stripped.append(rows[-1])
    body = ""
    for s in kept:
        body += "\n" if s == "\n" else ((" " if body and not body.endswith("\n") else "") + s)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    # LinkedIn note: any numbers in it must exist in the hook source
    li = draft.get("linkedin", "")
    hook_src = _norm_num(" ".join(c["title"] for c in cites[:1]))
    li_bad = [t for t in claim_terms(li, allow) if re.search(r"\d", t) and _norm_num(t) not in hook_src]
    if li_bad:
        li = f"Hi {_first(prospect['name'])}, I work on {seller.get('short') or 'finance automation'} at {seller['name']} and would be glad to connect."
    return {"body": body, "linkedin": li, "rows": rows, "stripped": stripped, "linkedin_fixed": bool(li_bad)}


# --------------------------------------------------------------------------- quality
def word_count(body: str) -> int:
    return len(re.findall(r"\b[\w'’$.,%-]+\b", re.sub(r"\[\d+\]", "", body)))


def quality_checks(*, subject: str, body: str, linkedin: str, grounding: dict, hook: dict | None, secondary: dict | None, persona: str, company: str) -> tuple[list[dict], int, dict]:
    wc = word_count(body)
    low = (subject + " " + body + " " + linkedin).lower()
    banned = [b for b in BANNED if b in low]
    sens = sorted({m.group(0).lower() for m in SENSITIVE_IN_DRAFT.finditer(body + " " + subject + " " + linkedin)})
    qmarks = body.count("?")
    checks = [
        {"name": "Every factual claim cites a source", "status": "pass" if not grounding["stripped"] else "warn",
         "detail": "All company-specific statements trace to a gathered source." if not grounding["stripped"] else f"{len(grounding['stripped'])} unsupported sentence(s) removed: " + "; ".join(r["sentence"][:70] for r in grounding["stripped"])},
        {"name": "Length 80-130 words", "status": "pass" if 80 <= wc <= 130 else ("warn" if 65 <= wc <= 150 else "fail"), "detail": f"{wc} words"},
        {"name": "Subject line is short and specific", "status": "pass" if len(subject) <= 60 and company.lower().split()[0] in subject.lower() else "warn", "detail": f"“{subject}” ({len(subject)} chars)"},
        {"name": "No generic filler phrases", "status": "pass" if not banned else "fail", "detail": "Checked against a list of %d banned phrases" % len(BANNED) if not banned else "Found: " + ", ".join(banned)},
        {"name": "No sensitive topics in copy", "status": "pass" if not sens else "fail", "detail": "No layoffs, legal, stock-sale, pay or headcount language" if not sens else "Found: " + ", ".join(sens)},
        {"name": "Single clear call to action", "status": "pass" if qmarks == 1 else "warn", "detail": f"{qmarks} question(s) in the body"},
        {"name": "LinkedIn note under 300 characters", "status": "pass" if len(linkedin) <= 300 else "fail", "detail": f"{len(linkedin)} chars" + (" (rewritten: unsupported detail)" if grounding.get("linkedin_fixed") else "")},
    ]
    # personalisation score (0-100), transparent breakdown
    parts = {
        "Company-specific hook": round(35 * (hook["score"] / 100)) if hook else 0,
        "Supporting signal": 15 if secondary else 0,
        "Role-specific angle": 15 if persona != "other" else 5,
        "Hook is recent (≤45 days or open role)": 10 if hook and (hook.get("kind") == "job" or (hook.get("days") or 999) <= 45) else 0,
        "Concrete detail (number / name) in opener": 15 if hook and re.search(r"\d|[A-Z][a-z]+ [A-Z]", body.split("\n\n")[1] if body.count("\n\n") >= 1 else body) else 0,
        "Clean copy (no filler, grounded)": 10 if not banned and not grounding["stripped"] else 0,
    }
    pscore = min(100, sum(parts.values()))
    checks.append({"name": "Personalisation score", "status": "pass" if pscore >= 65 else "warn", "detail": f"{pscore}/100 — " + ", ".join(f"{k} +{v}" for k, v in parts.items() if v)})
    return checks, pscore, parts


def type_label(t: str) -> str:
    return TYPE_LABEL.get(t, t)
