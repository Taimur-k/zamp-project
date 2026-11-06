"""PS-2 Vendor onboarding pipeline: submission -> APPROVED / PENDING / MANUAL_REVIEW / REJECTED.

Design rules (explained in README):
* Every stage always runs, even after a problem is found, so the vendor gets ONE
  complete list of what to fix instead of five rounds of back-and-forth.
* Every finding is classified by *who has to act*:
    vendor  -> the vendor must fix/provide something           -> PENDING
    review  -> ambiguous; a human in compliance must look      -> MANUAL_REVIEW
    reject  -> fraud / sanctions / irreconcilable              -> REJECTED
    warn    -> a risk signal; several together trigger review
* Precedence: REJECTED > MANUAL_REVIEW > PENDING > APPROVED.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from common import llm

from . import docs as D
from . import validators as V

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data"
SAMPLES = APP_DIR / "samples"
OUTBOX = APP_DIR / "outbox"
UPLOADS = APP_DIR / "uploads"
MASTER_CSV = DATA / "vendor_master.csv"
APPROVED_CSV = DATA / "vendor_master_approved.csv"
WATCHLIST_CSV = DATA / "watchlist.csv"
AUDIT_LOG = DATA / "audit_log.jsonl"
_WRITE_LOCK = threading.Lock()  # vendor-ID allocation and audit appends must not interleave

BUYER = "Acme Industries"
FROM_ADDR = "Supplier Onboarding <supplier-onboarding@acme-industries.example>"
RISK_TEAM = "Procurement Risk <procurement-risk@acme-industries.example>"
COMPLIANCE_TEAM = "Vendor Compliance <vendor-compliance@acme-industries.example>"

PLAN = [
    ("intake", "Intake & normalise"),
    ("completeness", "Completeness check"),
    ("documents", "Read documents & cross-check"),
    ("tax", "Tax ID validation"),
    ("bank", "Bank details validation"),
    ("risk", "Fraud, duplicate & sanctions screening"),
    ("decision", "Decision"),
    ("communicate", "Notify vendor & write outputs"),
]
STAGE_TITLES = dict(PLAN)

SCENARIOS = [
    {"id": "happy_us_northwind", "kind": "happy", "expected": "APPROVED",
     "title": "Clean US vendor - Northwind Analytics LLC",
     "description": "Complete W-9, bank letter, insurance and formation certificate. EIN prefix, ABA checksum and every document agree."},
    {"id": "happy_in_shakti", "kind": "happy", "expected": "APPROVED",
     "title": "Indian vendor with a scanned cheque - Shakti Precision",
     "description": "GSTIN checksum + state, PAN entity type, IFSC/SWIFT. The cancelled cheque is a photo (OCR). 'PRIVATE LIMITED' vs 'Pvt. Ltd.' must NOT be flagged."},
    {"id": "edge_parent_account_expired_insurance", "kind": "edge", "expected": "PENDING",
     "title": "Form looks perfect, documents don't - Kestrel FM",
     "description": "The form says the account is in Kestrel's name, but the bank letter shows the parent company. Insurance expired in March. Vendor gets one email listing both."},
    {"id": "edge_resubmission_kestrel", "kind": "edge", "expected": "APPROVED",
     "title": "Resubmission - Kestrel sends corrected documents",
     "description": "Links to the earlier Kestrel submission, re-checks everything and shows which issues were resolved. Run the Kestrel case first."},
    {"id": "edge_bank_account_in_use", "kind": "edge", "expected": "REJECTED",
     "title": "Bank account already belongs to another vendor - Brightline",
     "description": "Every document is internally consistent, but the account is already on file for Harborview Freight (V-1007). Fraud hold; the vendor is not told why."},
    {"id": "edge_gstin_typo_state", "kind": "edge", "expected": "PENDING",
     "title": "GSTIN that looks right but isn't - Meridian Agro",
     "description": "Correct 15-char format, but the check character fails and state code 29 (Karnataka) contradicts the Mumbai address. The GST certificate reveals the right number."},
    {"id": "edge_sanctions_near_match", "kind": "edge", "expected": "MANUAL_REVIEW",
     "title": "Sanctions near-match - Volkov Industrial Supplies GmbH",
     "description": "92% similar to a watch-listed entity in another country. Not auto-rejected (likely a false positive) - routed to compliance with the evidence."},
]

COUNTRY_ALIASES = {
    "USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "UK": "GB", "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB", "ENGLAND": "GB", "INDIA": "IN", "GERMANY": "DE", "DEUTSCHLAND": "DE", "FRANCE": "FR",
    "IRAN": "IR", "NORTH KOREA": "KP", "SYRIA": "SY", "CUBA": "CU", "NETHERLANDS": "NL", "IRELAND": "IE", "SPAIN": "ES", "ITALY": "IT", "BELGIUM": "BE", "AUSTRIA": "AT",
}
EU = {"DE", "FR", "NL", "IE", "ES", "IT", "BE", "AT"}
SUPPORTED = {"US", "GB", "IN"} | EU
EMBARGOED = {"IR": "Iran", "KP": "North Korea", "SY": "Syria", "CU": "Cuba"}
INSURANCE_CATEGORIES = {"onsite_services", "professional_services", "logistics", "construction"}
TAX_LABEL = {"US": "EIN (Employer Identification Number)", "GB": "VAT registration number", "IN": "GSTIN"}
RISK_REVIEW_THRESHOLD = 40
INSURANCE_WARN_DAYS = 30
NEW_ENTITY_DAYS = 180
REPLY_DAYS = 10        # vendor is asked to reply within this many days
REMINDER_DAYS = 3      # first automatic reminder due after this many days of silence
ESCALATE_DAYS = 7      # after this, the internal requester is asked to chase the vendor


# ====================================================================== context
class Ctx:
    def __init__(self, run):
        self.run = run
        self.sub: dict = {}
        self.checks: list[dict] = []
        self.issues: list[dict] = []
        self.facts: dict = {"doc_holders": [], "risk_signals": []}
        self.docs: list[dict] = []
        self.engine_notes: list[str] = []

    def issue(self, code, stage, severity, title, detail, request=None, why=None, internal=None, points=0):
        if any(i["code"] == code for i in self.issues):
            return
        self.issues.append({"code": code, "stage": stage, "stage_title": STAGE_TITLES.get(stage, stage), "severity": severity,
                            "title": title, "detail": detail, "request": request, "why": why, "internal": internal, "points": points})

    def by(self, sev):
        return [i for i in self.issues if i["severity"] == sev]


class StageChecks:
    """Records checks for one stage, logs them live, and sets the stage's final status."""

    LEVEL = {"pass": "ok", "fail": "error", "warn": "warn", "na": "info", "info": "info"}

    def __init__(self, ctx: Ctx, st, key: str):
        self.ctx, self.st, self.key = ctx, st, key
        self.mine: list[dict] = []

    def __call__(self, status, name, detail=""):
        c = {"stage": self.key, "stage_title": STAGE_TITLES[self.key], "status": status, "name": name, "detail": detail}
        self.ctx.checks.append(c)
        self.mine.append(c)
        mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "na": "N/A ", "info": "INFO"}[status]
        self.st.log(f"[{mark}] {name}" + (f" - {detail}" if detail else ""), self.LEVEL[status])
        return status == "pass"

    def finish(self, ok_note: str):
        fails = [c for c in self.mine if c["status"] == "fail"]
        warns = [c for c in self.mine if c["status"] == "warn"]
        passed = sum(1 for c in self.mine if c["status"] == "pass")
        self.st.data(checks_passed=passed, checks_failed=len(fails), warnings=len(warns))
        if fails:
            self.st.status("fail", f"{len(fails)} problem(s): " + "; ".join(c["name"] for c in fails[:3]))
        elif warns:
            self.st.status("warn", f"{passed} passed, {len(warns)} warning(s): " + "; ".join(c["name"] for c in warns[:2]))
        else:
            self.st.note(ok_note.format(n=passed))


# ====================================================================== helpers
def _s(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def flatten(raw: dict) -> dict:
    """Accept the nested form JSON (company/tax/contact/bank) or a flat dict."""
    c, a = raw.get("company") or {}, (raw.get("company") or {}).get("address") or {}
    t, ct, b = raw.get("tax") or {}, raw.get("contact") or {}, raw.get("bank") or {}

    def g(key, *srcs):
        for s in (raw, *srcs):
            if isinstance(s, dict) and s.get(key) not in (None, ""):
                return s.get(key)
        return ""

    return {
        "submission_id": _s(raw.get("submission_id")) or f"SUB-{datetime.now():%Y%m%d%H%M%S}",
        "submitted_at": _s(raw.get("submitted_at")), "resubmission_of": _s(raw.get("resubmission_of")),
        "vendor_type": _s(raw.get("vendor_type") or "company").lower(), "category": _s(raw.get("category") or "goods").lower(),
        "legal_name": _s(g("legal_name", c)), "trading_name": _s(g("trading_name", c)),
        "registration_number": _s(g("registration_number", c)), "country": _s(g("country", c)),
        "address_line1": _s(g("line1", a) or g("address_line1", c)), "city": _s(g("city", a)), "state": _s(g("state", a)),
        "postal_code": _s(g("postal_code", a)), "website": _s(g("website", c)), "tax_id": _s(g("tax_id", t)),
        "contact_name": _s(g("name", ct) or raw.get("contact_name")), "contact_title": _s(g("title", ct)),
        "contact_email": _s(g("email", ct) or raw.get("contact_email")).lower(), "contact_phone": _s(g("phone", ct)),
        "account_holder": _s(g("account_holder", b)), "bank_name": _s(g("bank_name", b)), "bank_country": _s(g("bank_country", b)),
        "currency": _s(g("currency", b)).upper(), "iban": _s(g("iban", b)), "account_number": _s(g("account_number", b)),
        "routing_number": _s(g("routing_number", b)), "ifsc": _s(g("ifsc", b)), "sort_code": _s(g("sort_code", b)),
        "swift": _s(g("swift", b)), "documents": raw.get("documents") or [],
    }


def norm_country(v: str) -> str:
    u = (v or "").strip().upper()
    return COUNTRY_ALIASES.get(u, u)


def resolve_doc(file: str) -> Path | None:
    for base in (APP_DIR, SAMPLES):
        p = (base / file).resolve()
        if str(p).startswith(str(APP_DIR)) and p.is_file():
            return p
    return None


def mask(acct: str) -> str:
    a = V.clean_id(acct)
    return ("*" * max(0, len(a) - 4)) + a[-4:] if a else ""


def tax_core(tax_id: str, country: str) -> str:
    t = V.clean_id(tax_id)
    if country in ({"GB"} | EU) and t.startswith(country):
        t = t[2:]
    return t


def text_has_name(name: str, text: str) -> tuple[float, str]:
    """How strongly does `name` appear in `text`? Tolerates OCR that drops spaces."""
    score, line = V.best_line_match(name, text)
    compact = re.sub(r"[^a-z0-9]", "", V.normalize_name(name))
    if compact and compact in re.sub(r"[^a-z0-9]", "", text.lower()):
        return 1.0, line or name
    return score, line


def load_master() -> list[dict]:
    rows = []
    for path, source in ((MASTER_CSV, "vendor master"), (APPROVED_CSV, "approved via this process")):
        if path.exists():
            with path.open(newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    r["_source"] = source
                    r["_bank_key"] = V.bank_key(r)
                    rows.append(r)
    return rows


def load_watchlist() -> list[dict]:
    with WATCHLIST_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_previous(store, submission_id: str) -> dict | None:
    """Most recent completed run of the submission being corrected."""
    with sqlite3.connect(store.db_path) as c:
        rows = c.execute("SELECT id, input_json, result_json, decision, created_at FROM runs "
                         "WHERE status='completed' ORDER BY created_at DESC, rowid DESC").fetchall()
    for rid, inp, res, dec, created in rows:
        try:
            i = json.loads(inp or "{}")
        except ValueError:
            continue
        if _s(i.get("submission_id")) == submission_id:
            return {"run_id": rid, "result": json.loads(res or "{}"), "decision": dec, "created_at": created}
    return None


# ====================================================================== pipeline
def pipeline(run, raw: dict) -> dict:
    ctx = Ctx(run)
    today = date.today()

    # ------------------------------------------------------------------ 1. intake
    with run.stage("intake", STAGE_TITLES["intake"]) as st:
        sub = flatten(raw)
        changes = []
        for k in ("country", "bank_country"):
            n = norm_country(sub[k])
            if n != sub[k]:
                changes.append(f"{k}: '{sub[k]}' -> '{n}'")
            sub[k] = n
        for k in ("iban", "swift", "ifsc", "tax_id"):
            n = V.clean_id(sub[k]) if k != "tax_id" else sub[k].upper()
            if n != sub[k]:
                changes.append(f"{k}: removed spaces/punctuation")
            sub[k] = n
        if not sub["bank_country"] and sub["country"]:
            sub["bank_country"] = sub["country"]
            changes.append("bank_country: defaulted to company country")
        ctx.sub = sub
        st.log(f"Submission {sub['submission_id']} from {sub['legal_name'] or '(no legal name)'} ({sub['country'] or '??'}), "
               f"type={sub['vendor_type']}, category={sub['category']}")
        for ch in changes:
            st.log("Normalised " + ch)
        resolved = []
        for d in sub["documents"] if isinstance(sub["documents"], list) else []:
            if isinstance(d, str):
                d = {"file": d, "type": "unknown"}
            if not isinstance(d, dict):
                st.log(f"Ignored a malformed document entry: {d!r}", "warn")
                continue
            p = resolve_doc(str(d.get("file") or ""))
            resolved.append({"declared": d.get("type") or "unknown", "file": d.get("file"), "path": p})
            st.log(f"Attachment: {d.get('file')} declared as '{d.get('type')}'" + ("" if p else "  -> FILE NOT FOUND"), "info" if p else "warn")
        ctx.facts["attachments"] = resolved
        # resubmission linking
        prev = None
        root = sub["submission_id"]
        if sub["resubmission_of"]:
            prev = find_previous(run.store, sub["resubmission_of"])
            if prev:
                root = (prev["result"] or {}).get("root_submission_id") or sub["resubmission_of"]
                st.log(f"Resubmission of {sub['resubmission_of']}: linked to run {prev['run_id']} "
                       f"(outcome {prev['decision']}, {len((prev['result'] or {}).get('issues', []))} open issue(s))", "ok")
            else:
                root = sub["resubmission_of"]
                st.log(f"Resubmission of {sub['resubmission_of']}, but no earlier run found in history - evaluating from scratch", "warn")
        ctx.facts["prev"], ctx.facts["root"] = prev, root
        st.data(submission_id=sub["submission_id"], legal_name=sub["legal_name"], country=sub["country"],
                attachments=len(resolved), resubmission_of=sub["resubmission_of"] or None,
                linked_run=prev["run_id"] if prev else None)
        st.note(f"{sub['legal_name'] or 'Unnamed vendor'} · {sub['country']} · {len(resolved)} document(s)"
                + (f" · resubmission of {sub['resubmission_of']}" if sub["resubmission_of"] else ""))

    s = ctx.sub
    country, bcountry = s["country"], s["bank_country"]

    # ------------------------------------------------------------------ 2. completeness
    with run.stage("completeness", STAGE_TITLES["completeness"]) as st:
        chk = StageChecks(ctx, st, "completeness")
        if country not in SUPPORTED:
            chk("fail", "Country has onboarding rules", f"'{country or 'blank'}' is not one of {', '.join(sorted(SUPPORTED))}")
            ctx.issue("COUNTRY_UNSUPPORTED", "completeness", "review", "No onboarding rules for this country",
                      f"Country '{country}' needs a manual compliance check (tax ID / bank formats unknown to the rules).",
                      internal=f"Configure rules for {country} or review manually.")
        tax_label = TAX_LABEL.get(country, "VAT ID" if country in EU else "Tax ID")
        req = [("legal_name", "Company legal name"), ("country", "Country of registration"), ("address_line1", "Registered address"),
               ("city", "City"), ("postal_code", "Postal / ZIP code"), ("tax_id", tax_label), ("contact_name", "Contact name"),
               ("contact_email", "Contact email"), ("account_holder", "Bank account holder name"), ("bank_name", "Bank name")]
        if s["vendor_type"] != "sole_proprietor":
            req.append(("registration_number", "Company registration number"))
        if country == "IN" or country == "US":
            req.append(("state", "State"))
        if bcountry == "US":
            req += [("routing_number", "ABA routing number"), ("account_number", "Bank account number")]
        elif bcountry == "IN":
            req += [("ifsc", "IFSC code"), ("account_number", "Bank account number")]
        elif bcountry == "GB":
            if not s["iban"]:
                req += [("sort_code", "Sort code"), ("account_number", "Bank account number")]
        elif bcountry in EU:
            req += [("iban", "IBAN"), ("swift", "SWIFT/BIC")]
        if bcountry and country and bcountry != country:
            req.append(("swift", "SWIFT/BIC (cross-border account)"))
        seen: set = set()
        req = [(k, lab) for k, lab in req if not (k in seen or seen.add(k))]  # e.g. SWIFT required twice (EU + cross-border)
        missing = [lab for k, lab in req if not s.get(k)]
        for k, lab in req:
            if not s.get(k):
                chk("fail", f"{lab} provided", "missing")
                ctx.issue(f"MISSING_{k.upper()}", "completeness", "vendor", f"{lab} missing", f"The form has no {lab.lower()}.",
                          request=f"Please provide your {lab}.", why="It's a required field for setting up a supplier in your country.")
        if not missing:
            chk("pass", f"All {len(req)} required fields present", f"rules for country={country}, bank={bcountry}, type={s['vendor_type']}")
        if s["contact_email"]:
            if V.email_valid(s["contact_email"]):
                chk("pass", "Contact email is well-formed", s["contact_email"])
            else:
                chk("fail", "Contact email is well-formed", s["contact_email"])
                ctx.issue("BAD_EMAIL", "completeness", "vendor", "Contact email invalid", f"'{s['contact_email']}' is not a valid address.",
                          request="Please give us a valid contact email address.", why="We send remittance advice and onboarding updates there.")
        need_docs = ["tax_document", "bank_proof"]
        if s["vendor_type"] != "sole_proprietor":
            need_docs.append("incorporation")
        if s["category"] in INSURANCE_CATEGORIES:
            need_docs.append("insurance")
        declared = {a["declared"] for a in ctx.facts["attachments"] if a["path"]}
        ctx.facts["need_docs"] = need_docs
        for d in need_docs:
            if d in declared:
                chk("pass", f"Document attached: {D.DOC_TYPES[d]}", "")
            else:
                chk("fail", f"Document attached: {D.DOC_TYPES[d]}", "not attached" + (" (insurance is required for category "
                                                                                       f"'{s['category']}')" if d == "insurance" else ""))
                ctx.issue(f"DOC_MISSING_{d.upper()}", "completeness", "vendor", f"{D.DOC_TYPES[d]} missing", "Required document not attached.",
                          request=f"Please attach your {D.DOC_TYPES[d].lower()}.", why=_doc_why(d))
        for a in ctx.facts["attachments"]:
            if not a["path"]:
                chk("fail", f"Attachment readable: {a['file']}", "file not found")
        st.data(required_fields=len(req), missing_fields=missing, required_documents=need_docs)
        chk.finish("All {n} completeness checks passed")

    # ------------------------------------------------------------------ 3. documents
    with run.stage("documents", STAGE_TITLES["documents"]) as st:
        chk = StageChecks(ctx, st, "documents")
        use_llm = llm.available()
        st.log(f"Field extraction engine: {'Claude (fills gaps the rules miss) + rules' if use_llm else 'rules (label-based extraction)'}; "
               "text via PDF text layer, OCR for scans")
        detected_types = set()
        for a in ctx.facts["attachments"]:
            if not a["path"]:
                continue
            p: Path = a["path"]
            text, method = D.extract_text(p)
            dtype, scores = D.classify(text)
            fields = D.extract_fields(text)
            if use_llm and dtype:
                fields = _llm_fill(p, dtype, fields, st)
            rec = {"file": a["file"], "path": _rel(p), "declared": a["declared"], "detected": dtype, "method": method, "chars": len(text), "fields": {k: v for k, v in fields.items() if v}}
            ctx.docs.append(rec)
            st.log(f"{p.name}: {len(text)} chars via {method}; looks like '{dtype or 'unknown'}'")
            if not dtype:
                chk("fail", f"{p.name}: recognisable document", f"could not tell what this is (signals {scores})")
                ctx.issue(f"DOC_UNRECOGNISED_{p.stem.upper()}", "documents", "vendor", f"Couldn't read {p.name}",
                          "The file is unreadable or not a recognised document type.",
                          request=f"The file '{p.name}' couldn't be read or doesn't look like a {D.DOC_TYPES.get(a['declared'], 'supporting document').lower()}. Please send a clear copy.",
                          why="We verify every document against the details in your form.")
                continue
            if a["declared"] in D.DOC_TYPES and dtype != a["declared"]:
                chk("fail", f"{p.name}: is the document it claims to be",
                    f"uploaded as '{D.DOC_TYPES[a['declared']]}' but its content is a '{D.DOC_TYPES[dtype]}'")
                ctx.issue(f"DOC_WRONG_{a['declared'].upper()}", "documents", "vendor", f"Wrong document attached as {D.DOC_TYPES[a['declared']].lower()}",
                          f"{p.name} was uploaded as {D.DOC_TYPES[a['declared']].lower()} but is a {D.DOC_TYPES[dtype].lower()}.",
                          request=f"The file you sent as your {D.DOC_TYPES[a['declared']].lower()} ('{p.name}') is actually a {D.DOC_TYPES[dtype].lower()}. Please attach the correct document.",
                          why=_doc_why(a["declared"]))
            else:
                chk("pass", f"{p.name}: is a {D.DOC_TYPES[dtype].lower()}", f"via {method}")
            detected_types.add(dtype)
            _cross_check(ctx, chk, dtype, p.name, text, fields, today)
        for d in ctx.facts.get("need_docs", []):
            declared_ok = any(x["declared"] == d for x in ctx.facts["attachments"] if x["path"])
            if declared_ok and d not in detected_types:
                ctx.issue(f"DOC_MISSING_{d.upper()}", "documents", "vendor", f"{D.DOC_TYPES[d]} missing", "No valid document of this type after reading the files.",
                          request=f"Please attach your {D.DOC_TYPES[d].lower()}.", why=_doc_why(d))
        st.data(documents=[{k: v for k, v in r.items() if k != "fields"} | {"extracted": r["fields"]} for r in ctx.docs])
        chk.finish("{n} cross-checks passed - documents agree with the form")

    # ------------------------------------------------------------------ 4. tax
    with run.stage("tax", STAGE_TITLES["tax"]) as st:
        chk = StageChecks(ctx, st, "tax")
        reasons = []
        if s["tax_id"] and country in SUPPORTED:
            results = V.validate_tax_id(country, s["tax_id"], s["vendor_type"])
            for r in results:
                chk("pass" if r["ok"] else "fail", r["name"], r["detail"])
                if not r["ok"]:
                    reasons.append(r["bad"])
            if country == "IN" and all(r["ok"] for r in results if r["kind"] == "format"):
                ok, det = V.gstin_state_vs_address(s["tax_id"], s["state"])
                chk("pass" if ok else "fail", "GSTIN state code matches the registered address", det)
                if not ok:
                    code = V.clean_id(s["tax_id"])[:2]
                    reasons.append(f"its state code {code} is {V.GST_STATES.get(code)}, but your address is in {s['state']}")
                pan = V.clean_id(s["tax_id"])[2:12]
                first = (V.normalize_name(s["legal_name"]) or " ")[0].upper()
                if s["vendor_type"] != "sole_proprietor":
                    chk("pass" if pan[4] == first else "warn", "PAN 5th character matches first letter of the company name",
                        f"PAN {pan}: '{pan[4]}' vs '{first}' ({s['legal_name']})")
            if country == "GB" and s["registration_number"]:
                okc = bool(re.fullmatch(r"(\d{8}|[A-Z]{2}\d{6})", V.clean_id(s["registration_number"])))
                chk("pass" if okc else "warn", "Companies House number format (8 chars)", s["registration_number"])
            doc_tid = ctx.facts.get("doc_tax_id")
            if doc_tid and doc_tid != tax_core(s["tax_id"], country):
                doc_ok = all(r["ok"] for r in V.validate_tax_id(country, doc_tid, s["vendor_type"]))
                chk("fail", "Tax ID on form matches the tax certificate",
                    f"form {s['tax_id']} vs certificate {doc_tid}" + (" (the certificate's number is valid)" if doc_ok else ""))
                ctx.facts["doc_tax_id_valid"] = doc_ok
                reasons.append(f"it doesn't match the number on your tax document ({ctx.facts.get('doc_tax_id_display') or doc_tid})")
        elif s["tax_id"]:
            chk("na", "Tax ID validation", f"no rules for country '{country}'")
        else:
            chk("na", "Tax ID validation", "no tax ID supplied (see completeness)")
        if reasons:
            label = TAX_LABEL.get(country, "tax ID")
            doc_tid = ctx.facts.get("doc_tax_id")
            shown = ctx.facts.get("doc_tax_id_display") or doc_tid
            hint = (f" Your tax document shows {shown}, which is valid - if that is your correct {label}, just reply confirming it."
                    if doc_tid and ctx.facts.get("doc_tax_id_valid") else "")
            ctx.issue("TAXID_INVALID", "tax", "vendor", f"{label} failed validation",
                      f"{s['tax_id']}: " + "; ".join(reasons),
                      request=f"Please check the {label} on your form ({s['tax_id']}): it " + "; and ".join(reasons[:3]) + "." + hint,
                      why="We must report the correct tax registration on every payment; an invalid number means invoices can't be booked.",
                      internal="Likely a typing error rather than fraud: the certificate is consistent with the address." if hint else None)
        st.data(tax_id=s["tax_id"], country=country, problems=reasons or None)
        chk.finish("Tax ID valid - {n} checks passed (format, checksum, consistency)")

    # ------------------------------------------------------------------ 5. bank
    with run.stage("bank", STAGE_TITLES["bank"]) as st:
        chk = StageChecks(ctx, st, "bank")
        bank_problems = []
        sw = V.swift_parse(s["swift"]) if s["swift"] else None
        if s["swift"]:
            if sw:
                chk("pass" if sw["country"] == bcountry else "fail", "SWIFT/BIC format and country",
                    f"{sw['swift']}: bank {sw['bank']}, country {sw['country']} (bank country {bcountry})")
                if sw["country"] != bcountry:
                    bank_problems.append(f"the SWIFT/BIC {s['swift']} belongs to a bank in {sw['country']}, not {bcountry}")
            else:
                chk("fail", "SWIFT/BIC format", f"'{s['swift']}' is not 8 or 11 characters of the form BANKCCLL[BBB]")
                bank_problems.append(f"the SWIFT/BIC '{s['swift']}' is not a valid format")
        if s["iban"]:
            ok, det = V.iban_valid(s["iban"])
            chk("pass" if ok else "fail", "IBAN checksum (ISO 13616 mod-97)", f"{s['iban']}: {det}")
            if not ok:
                bank_problems.append(f"the IBAN fails its check digits ({det})")
            elif s["iban"][:2] != bcountry:
                chk("fail", "IBAN country matches bank country", f"IBAN is {s['iban'][:2]}, bank country {bcountry}")
                bank_problems.append(f"the IBAN is from {s['iban'][:2]} but the bank country is {bcountry}")
            else:
                chk("pass", "IBAN country matches bank country", bcountry)
            if ok and s["iban"].startswith("GB"):
                emb_sort, emb_acct = s["iban"][8:14], s["iban"][14:]
                if s["sort_code"] or s["account_number"]:
                    same = (not s["sort_code"] or V.sort_code_clean(s["sort_code"]) == emb_sort) and (not s["account_number"] or D.digits(s["account_number"]) == emb_acct)
                    chk("pass" if same else "fail", "Sort code / account number agree with the IBAN",
                        f"IBAN embeds {emb_sort} / {emb_acct}; form says {s['sort_code']} / {s['account_number']}")
                    if not same:
                        bank_problems.append("the sort code/account number don't match the IBAN")
            if ok and sw and s["iban"][:2] in ("GB",) and s["iban"][4:8] != sw["bank"]:
                chk("warn", "IBAN bank code matches SWIFT bank", f"IBAN bank {s['iban'][4:8]} vs SWIFT {sw['bank']}")
        if bcountry == "US":
            if s["routing_number"]:
                ok, det = V.aba_valid(s["routing_number"])
                chk("pass" if ok else "fail", "ABA routing number checksum", f"{s['routing_number']}: {det}")
                if not ok:
                    bank_problems.append(f"the routing number {s['routing_number']} is not valid ({det})")
            if s["account_number"]:
                okf = bool(re.fullmatch(r"\d{4,17}", D.digits(s["account_number"])))
                chk("pass" if okf else "fail", "US account number length (4-17 digits)", mask(s["account_number"]))
        if bcountry == "IN":
            if s["ifsc"]:
                ok, det = V.ifsc_valid(s["ifsc"])
                chk("pass" if ok else "fail", "IFSC code format", det)
                if not ok:
                    bank_problems.append(f"the IFSC code {s['ifsc']} is not valid")
                else:
                    code = s["ifsc"][:4]
                    if sw:
                        exp = V.IFSC_TO_SWIFT.get(code)
                        same = exp == sw["bank"] if exp else True
                        chk("pass" if same else "fail", "IFSC bank matches SWIFT bank", f"IFSC {code} -> SWIFT {exp or '?'}; given {sw['bank']}")
                        if not same:
                            bank_problems.append(f"the IFSC ({code}) and SWIFT ({sw['bank']}) point to different banks")
                    bn = V.IFSC_BANK_NAMES.get(code)
                    if bn and s["bank_name"]:
                        sim = V.name_similarity(bn, s["bank_name"])
                        chk("pass" if sim >= 0.7 or V.normalize_name(bn) in V.normalize_name(s["bank_name"]) else "warn",
                            "Bank name matches the IFSC bank", f"IFSC {code} = {bn}; form says '{s['bank_name']}'")
            if s["account_number"]:
                okf = bool(re.fullmatch(r"\d{9,18}", D.digits(s["account_number"])))
                chk("pass" if okf else "fail", "Indian account number length (9-18 digits)", mask(s["account_number"]))
        if bcountry == "GB" and not s["iban"] and s["sort_code"]:
            okf = len(V.sort_code_clean(s["sort_code"])) == 6 and len(D.digits(s["account_number"])) == 8
            chk("pass" if okf else "fail", "UK sort code (6) and account number (8 digits)", f"{s['sort_code']} / {mask(s['account_number'])}")
            if not okf:
                bank_problems.append("the sort code or account number has the wrong number of digits")
        if bank_problems:
            ctx.issue("BANK_DETAILS_INVALID", "bank", "vendor", "Bank details failed validation", "; ".join(bank_problems),
                      request="Please re-check your bank details: " + "; ".join(bank_problems) + ".",
                      why="A payment to an invalid account is rejected by the bank or, worse, lands in the wrong account.")
        if country and bcountry and country != bcountry:
            chk("warn", "Bank account is in the vendor's country", f"company in {country}, account in {bcountry}")
            ctx.facts["risk_signals"].append(("Bank account in a different country from the company", 20))
        # --- account holder name: the cross-reference that catches the most real problems
        _holder_check(ctx, chk)
        st.data(bank_key=V.bank_key(s), account=mask(s["iban"] or s["account_number"]), holder_form=s["account_holder"],
                holder_on_documents=[h for _, h in ctx.facts["doc_holders"]] or None)
        chk.finish("Bank details valid - {n} checks passed, holder name matches the legal entity")

    # ------------------------------------------------------------------ 6. risk
    with run.stage("risk", STAGE_TITLES["risk"]) as st:
        chk = StageChecks(ctx, st, "risk")
        # sanctions / watchlist
        wl = load_watchlist()
        names = {"legal name": s["legal_name"], "trading name": s["trading_name"], "account holder": s["account_holder"]}
        for _, h in ctx.facts["doc_holders"]:
            names.setdefault("holder on bank document", h)
        hits = []
        for role, nm in names.items():
            if not nm:
                continue
            for w in wl:
                for alias in [w["name"], *[x for x in w["aliases"].split("|") if x]]:
                    sim = V.name_similarity(nm, alias)
                    if sim >= 0.85:
                        hits.append({"role": role, "name": nm, "listed": alias, "list_id": w["list_id"], "country": w["country"],
                                     "program": w["program"], "similarity": sim, "notes": w["notes"]})
        st.log(f"Screened {len([n for n in names.values() if n])} name(s) against {len(wl)} watch-list entries (incl. aliases)")
        best = {}
        for h in sorted(hits, key=lambda h: -h["similarity"]):
            best.setdefault(h["list_id"], h)
        if not best:
            chk("pass", "Sanctions / watch-list screening", "no name within 85% of a listed entity")
        for h in best.values():
            det = (f"{h['role']} '{h['name']}' is {round(h['similarity'] * 100)}% similar to listed '{h['listed']}' "
                   f"({h['list_id']}, {h['program']}, {h['country']})")
            if h["similarity"] >= 0.999:
                chk("fail", "Sanctions screening: exact match", det)
                ctx.issue("SANCTIONS_MATCH", "risk", "reject", "Exact sanctions match", det,
                          internal=f"Exact match to {h['list_id']}. Do not engage; escalate to compliance/legal. Do not disclose the reason to the vendor.")
            else:
                chk("warn", "Sanctions screening: possible match", det)
                diffs = []
                if h["country"] != country:
                    diffs.append(f"listed entity is in {h['country']}, vendor is in {country}")
                ctx.issue("SANCTIONS_NEAR_MATCH", "risk", "review", "Possible sanctions match - needs a human", det,
                          internal=(f"Near match ({round(h['similarity'] * 100)}%) - not auto-rejected because fuzzy name matches produce false positives. "
                                    f"{'; '.join(diffs) + '. ' if diffs else ''}Verify: registration number {s['registration_number'] or '-'}, "
                                    f"beneficial owners/directors, address, and whether the entity is a subsidiary or alias of {h['listed']}. "
                                    f"List note: {h['notes'] or '-'}."))
        for c_, label in ((country, "company"), (bcountry, "bank")):
            if c_ in EMBARGOED:
                chk("fail", f"Embargoed jurisdiction ({label})", EMBARGOED[c_])
                ctx.issue(f"EMBARGO_{label.upper()}", "risk", "reject", "Embargoed jurisdiction", f"{label} country is {EMBARGOED[c_]}",
                          internal="Comprehensive embargo - onboarding not permitted.")
        # duplicates against the vendor master
        master = load_master()
        my_key, my_tax = V.bank_key(s), V.clean_id(s["tax_id"])
        root = ctx.facts["root"]
        st.log(f"Checking {len(master)} existing vendor record(s) for re-used bank accounts and tax IDs (bank key {my_key})")
        dup_found = False
        for r in master:
            if r.get("root_submission_id") and r["root_submission_id"] == root:
                ctx.facts["existing_record"] = r
                st.log(f"{r['vendor_id']} was created from this same onboarding case ({root}) - not a conflict", "info")
                continue
            same_name = V.name_similarity(r["legal_name"], s["legal_name"]) >= 0.9
            if my_key and r["_bank_key"] == my_key:
                dup_found = True
                det = f"account {mask(s['iban'] or s['account_number'])} is already on file for {r['vendor_id']} {r['legal_name']} ({r['_source']}, {r['status']})"
                if same_name:
                    chk("warn", "Bank account not already used by another vendor", det + " - same company, possible duplicate record")
                    ctx.issue("DUPLICATE_VENDOR", "risk", "review", "Possible duplicate of an existing vendor", det,
                              internal=f"Same company and account as {r['vendor_id']}. Confirm with the requester whether this is a duplicate; reactivate instead of creating a new record.")
                else:
                    chk("fail", "Bank account not already used by another vendor", det)
                    ctx.issue("BANK_ACCOUNT_IN_USE", "risk", "reject", "Bank account belongs to a different vendor", det,
                              internal=(f"Payment-diversion pattern: a new vendor ({s['legal_name']}) submitting the bank account of an existing vendor "
                                        f"({r['vendor_id']} {r['legal_name']}). Fraud hold. Call {r['vendor_id']} on the phone number held on file "
                                        "(NOT the contact in this submission) to confirm their account is unchanged, and review recent bank-detail change requests."))
            if my_tax and V.clean_id(r.get("tax_id")) == my_tax:
                dup_found = True
                det = f"tax ID {s['tax_id']} is already on file for {r['vendor_id']} {r['legal_name']} ({r['_source']})"
                if same_name:
                    chk("warn", "Tax ID not already registered", det + " - same company, possible duplicate")
                    ctx.issue("DUPLICATE_VENDOR", "risk", "review", "Possible duplicate of an existing vendor", det,
                              internal=f"Vendor appears to exist as {r['vendor_id']}. Avoid a duplicate record (duplicate payments risk).")
                else:
                    chk("fail", "Tax ID not already registered to a different company", det)
                    ctx.issue("TAXID_IN_USE", "risk", "review", "Tax ID belongs to another vendor on file", det,
                              internal=f"Could be a rename/acquisition of {r['vendor_id']} or impersonation. Ask for proof of name change before proceeding.")
        if not dup_found:
            chk("pass", "No duplicate bank account or tax ID in the vendor master", f"{len(master)} records checked")
        # contact / domain signals
        email_dom = s["contact_email"].split("@")[-1] if "@" in s["contact_email"] else ""
        site_dom = V.registrable(V.domain_of_url(s["website"])) if s["website"] else ""
        if email_dom in V.FREE_EMAIL:
            chk("warn", "Contact uses a company email domain", f"{email_dom} is a free webmail provider")
            ctx.facts["risk_signals"].append((f"Free webmail contact ({email_dom})", 15))
        elif email_dom and site_dom and V.registrable(email_dom) != site_dom:
            chk("warn", "Contact email domain matches the website", f"email @{email_dom} vs website {site_dom}")
            ctx.facts["risk_signals"].append((f"Email domain {email_dom} differs from website {site_dom}", 20))
        elif email_dom:
            chk("pass", "Contact email domain matches the company website", f"@{email_dom}")
        if not s["website"]:
            ctx.facts["risk_signals"].append(("No company website given", 5))
        signals = ctx.facts["risk_signals"]
        score = sum(p for _, p in signals)
        for name, pts in signals:
            st.log(f"Risk signal +{pts}: {name}", "warn")
        if score >= RISK_REVIEW_THRESHOLD:
            chk("warn", "Combined risk score below review threshold", f"score {score} >= {RISK_REVIEW_THRESHOLD}")
            ctx.issue("RISK_SCORE", "risk", "review", "Several risk signals together", f"Risk score {score}: " + "; ".join(n for n, _ in signals),
                      internal="No single signal is disqualifying, but together they warrant a call-back verification before first payment.")
        else:
            chk("pass", "Combined risk score below review threshold", f"score {score} (threshold {RISK_REVIEW_THRESHOLD})")
        ctx.facts["risk_score"] = score
        st.data(risk_score=score, signals=[f"+{p} {n}" for n, p in signals] or None,
                watchlist_hits=list(best.values()) or None, master_records_checked=len(master))
        chk.finish("No sanctions, duplicate or fraud signals ({n} checks)")

    # ------------------------------------------------------------------ 7. decision
    with run.stage("decision", STAGE_TITLES["decision"]) as st:
        prev = ctx.facts.get("prev")
        prev_review = ((prev or {}).get("result") or {}).get("review")
        if prev_review:
            st.log(f"Previous run was decided by reviewer {prev_review['reviewer']}: {prev_review['action']} - \"{prev_review['note']}\"", "info")
            if prev_review["action"] == "request_info":
                # Only the human who asked can judge whether the answer is good enough; the rules can't.
                ctx.issue("REVIEWER_FOLLOWUP", "decision", "review", "Reviewer asked for more information on the previous submission",
                          f"{prev_review['reviewer']} asked the vendor: \"{prev_review.get('vendor_message') or prev_review['note']}\"",
                          internal=f"Confirm this resubmission answers the question before approving. Reviewer's note: {prev_review['note']}")
        rej, rev, ven = ctx.by("reject"), ctx.by("review"), ctx.by("vendor")
        n_checks = len(ctx.checks)
        n_pass = sum(1 for c in ctx.checks if c["status"] == "pass")
        n_warn = sum(1 for c in ctx.checks if c["status"] == "warn")
        if rej:
            decision = "REJECTED"
            headline = f"Rejected: {rej[0]['title'].lower()}" + (f" (+{len(rej) - 1} more)" if len(rej) > 1 else "") + "."
            reason = rej[0]["detail"]
        elif rev:
            decision = "MANUAL_REVIEW"
            headline = f"Held for compliance review: {rev[0]['title'].lower()}."
            reason = rev[0]["detail"] + (f" Also {len(ven)} item(s) requested from the vendor." if ven else "")
        elif ven:
            decision = "PENDING"
            headline = f"Pending: {len(ven)} thing{'s' if len(ven) > 1 else ''} needed from the vendor before approval."
            reason = "; ".join(i["title"] for i in ven) + "."
        else:
            decision = "APPROVED"
            headline = f"Approved: {s['legal_name']} passed all {n_pass} checks and can be paid."
            reason = "Form, documents, tax ID and bank details all agree; no fraud, duplicate or sanctions signals." + (
                f" {n_warn} minor warning(s) noted for the file." if n_warn else "")
        st.log("Rule: any REJECT finding -> REJECTED; else any REVIEW -> MANUAL_REVIEW; else any VENDOR item -> PENDING; else APPROVED")
        st.log(f"Findings: {len(rej)} reject, {len(rev)} review, {len(ven)} vendor-fixable; {n_pass}/{n_checks} checks passed", "info")
        for i in ctx.issues:
            st.log(f"[{i['severity'].upper()}] {i['title']}: {i['detail']}", "error" if i["severity"] == "reject" else "warn")
        st.log(f"Decision: {decision} - {headline}", "ok" if decision == "APPROVED" else "warn")
        resub = None
        if prev:
            # reviewer requests can't be "resolved" by the rules - they are carried as REVIEWER_FOLLOWUP above
            prev_issues = {i["code"]: i for i in (prev["result"] or {}).get("issues", []) if not i["code"].startswith("REVIEWER_")}
            now = {i["code"] for i in ctx.issues}
            resub = {"previous_run": prev["run_id"], "previous_decision": prev["decision"],
                     "resolved": [prev_issues[c]["title"] for c in prev_issues if c not in now],
                     "still_open": [prev_issues[c]["title"] for c in prev_issues if c in now],
                     "new": [i["title"] for i in ctx.issues if i["code"] not in prev_issues]}
            st.log(f"Compared with run {prev['run_id']}: {len(resub['resolved'])} resolved, {len(resub['still_open'])} still open, {len(resub['new'])} new")
        st.data(decision=decision, reason=reason, reject=len(rej), review=len(rev), vendor_items=len(ven), resubmission=resub)
        st.note(headline)
        if decision in ("PENDING", "MANUAL_REVIEW"):
            st.status("warn", headline)
        elif decision == "REJECTED":
            st.status("fail", headline)

    # ------------------------------------------------------------------ 8. communicate
    with run.stage("communicate", STAGE_TITLES["communicate"]) as st:
        outputs, email, internal_email = [], None, None
        vendor_id = None
        OUTBOX.mkdir(exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}_{run.id}"
        if decision == "APPROVED":
            existing = ctx.facts.get("existing_record")
            if existing:
                vendor_id = existing["vendor_id"]
                st.log(f"Vendor already created as {vendor_id} from this onboarding case - not writing a duplicate record (idempotent)", "ok")
                outputs.append({"label": f"Existing vendor record {vendor_id} (no duplicate written)", "path": _rel(APPROVED_CSV)})
            else:
                vendor_id = _append_approved(s, ctx.facts["root"], run.id)
                st.log(f"Created vendor {vendor_id} in {_rel(APPROVED_CSV)} - payment details now usable for invoices", "ok")
                outputs.append({"label": f"Vendor master record {vendor_id}", "path": _rel(APPROVED_CSV)})
        subject, body = _vendor_email(decision, s, ven, vendor_id, today)
        body, engine = _polish(subject, body, decision, st)
        email = _write_eml(OUTBOX / f"{stamp}_vendor.eml", s["contact_email"] or "unknown@vendor", subject, body)
        email["engine"] = engine
        outputs.append({"label": "Email to vendor (.eml)", "path": email["path"]})
        st.log(f"Wrote vendor email '{subject}' -> {email['path']}", "ok")
        notes = _reviewer_notes(ctx)
        if decision in ("REJECTED", "MANUAL_REVIEW"):
            team = RISK_TEAM if decision == "REJECTED" else COMPLIANCE_TEAM
            isubj = f"[{'FRAUD HOLD' if decision == 'REJECTED' else 'REVIEW'}] {s['legal_name']} ({s['submission_id']}) - {headline}"
            ibody = (f"Automated onboarding run {run.id} needs a human.\n\nVendor: {s['legal_name']} ({country})\nSubmission: {s['submission_id']}\n"
                     f"Decision: {decision}\n\nWhy:\n" + "\n".join(f"- {n}" for n in notes) +
                     "\n\nThe vendor has NOT been told the reason." + ("" if decision == "MANUAL_REVIEW" else " Do not contact them using details from this submission."))
            internal_email = _write_eml(OUTBOX / f"{stamp}_internal.eml", team, isubj, ibody)
            outputs.append({"label": f"Internal escalation to {team.split('<')[0].strip()} (.eml)", "path": internal_email["path"]})
            st.log(f"Escalated to {team} -> {internal_email['path']}", "warn")
        reply_by = (today + timedelta(days=REPLY_DAYS)).isoformat() if decision == "PENDING" else None
        audit({"event": "automated_decision", "run_id": run.id, "case_id": ctx.facts["root"], "submission_id": s["submission_id"],
               "vendor": s["legal_name"], "decision": decision, "by": "automated rules",
               "findings": [f"{i['severity']}:{i['code']}" for i in ctx.issues], "vendor_id": vendor_id,
               "emails": [e["path"] for e in (email, internal_email) if e]})
        outputs.append({"label": "Audit log entry (append-only)", "path": _rel(AUDIT_LOG)})
        st.log(f"Audit entry appended to {_rel(AUDIT_LOG)}", "ok")
        if reply_by:
            st.log(f"Follow-up scheduled: reminder after {REMINDER_DAYS} days, escalation after {ESCALATE_DAYS}, reply due {reply_by}")
        st.data(outputs=outputs, email_engine=engine, reply_by=reply_by)
        st.note(f"{len(outputs)} output(s) written" + (f" · vendor ID {vendor_id}" if vendor_id else ""))

    summary = headline
    return {
        "normalised_submission": {k: v for k, v in s.items() if k != "documents"},
        "reply_by": reply_by,
        "decision_history": [{"decision": decision, "by": "automated rules", "at": _utc_now(), "note": headline}],
        "decision": decision, "summary": summary, "headline": headline, "reason": reason,
        "vendor": {k: s[k] for k in ("submission_id", "legal_name", "trading_name", "country", "category", "contact_name", "contact_email")}
                  | {"bank_country": bcountry, "account": mask(s["iban"] or s["account_number"])},
        "vendor_id": vendor_id, "root_submission_id": ctx.facts["root"],
        "checks": ctx.checks, "issues": ctx.issues,
        "vendor_requests": [{"title": i["title"], "request": i["request"], "why": i["why"]} for i in ven],
        "reviewer_notes": notes, "risk": {"score": ctx.facts.get("risk_score", 0), "signals": [f"+{p} {n}" for n, p in ctx.facts["risk_signals"]]},
        "documents": ctx.docs, "email": email, "internal_email": internal_email, "outputs": outputs, "resubmission": resub,
        "stats": {"checks": len(ctx.checks), "passed": sum(1 for c in ctx.checks if c["status"] == "pass"),
                  "failed": sum(1 for c in ctx.checks if c["status"] == "fail"), "warnings": sum(1 for c in ctx.checks if c["status"] == "warn")},
    }


# ====================================================================== stage helpers
def _doc_why(d: str) -> str:
    return {
        "tax_document": "We need it to confirm your tax registration matches the company we are paying.",
        "bank_proof": "A bank-issued document is how we confirm the account really belongs to your company before sending money.",
        "incorporation": "It proves the legal entity exists and is the one named on the contract.",
        "insurance": "Our policy requires current liability insurance for suppliers working on-site or providing services.",
    }.get(d, "It is required for supplier set-up.")


def _cross_check(ctx: Ctx, chk, dtype: str, fname: str, text: str, f: dict, today: date):
    s = ctx.sub
    country = s["country"]
    if dtype in ("tax_document", "incorporation", "insurance") and s["legal_name"]:
        if f.get("name"):
            sim, shown = V.name_similarity(s["legal_name"], f["name"]), f["name"]
        else:
            sim, shown = text_has_name(s["legal_name"], text)
        if sim >= 0.85:
            chk("pass", f"{fname}: name matches legal name", f"'{shown}' ~ '{s['legal_name']}' ({round(sim * 100)}%)")
        else:
            chk("fail", f"{fname}: name matches legal name", f"document says '{shown or '?'}', form says '{s['legal_name']}' ({round(sim * 100)}%)")
            ctx.issue(f"DOC_NAME_{dtype.upper()}", "documents", "vendor", f"{D.DOC_TYPES[dtype]} is in a different name",
                      f"{fname} names '{shown}', not '{s['legal_name']}'.",
                      request=f"Your {D.DOC_TYPES[dtype].lower()} is issued to '{shown}', but you are registering '{s['legal_name']}'. "
                              f"Please send the document for {s['legal_name']}, or explain the relationship between the two names.",
                      why="Every document must belong to the same legal entity we contract with and pay.")
    if dtype == "tax_document" and s["tax_id"]:
        core = tax_core(s["tax_id"], country)
        if core and core in D.alnum(text):
            chk("pass", f"{fname}: shows the same tax ID as the form", s["tax_id"])
        else:
            shown = tax_core(D.alnum(f.get("tax_id")), country)
            ctx.facts["doc_tax_id"] = shown or None
            ctx.facts["doc_tax_id_display"] = f.get("tax_id")
            chk("fail", f"{fname}: shows the same tax ID as the form", f"form {s['tax_id']}, document {f.get('tax_id') or 'not found'}")
            if not shown:
                ctx.issue("TAXID_NOT_ON_DOC", "documents", "vendor", "Tax ID not found on the tax document",
                          f"{s['tax_id']} does not appear in {fname}.",
                          request=f"We couldn't find your tax ID {s['tax_id']} on '{fname}'. Please send the registration certificate that shows it.",
                          why="We match the tax ID on the form to an official document.")
    elif dtype == "bank_proof":
        acct = D.alnum(s["iban"]) if s["iban"] else D.digits(s["account_number"])
        hay = D.alnum(text) if s["iban"] else D.digits(text)
        if not acct:
            chk("na", f"{fname}: account number matches the form", "no account number on form")
        elif acct in hay or (s["iban"] and D.digits(s["account_number"]) and D.digits(s["account_number"]) in D.digits(text)):
            chk("pass", f"{fname}: account number matches the form", mask(acct))
        else:
            shown = f.get("iban") or f.get("account")
            chk("fail", f"{fname}: account number matches the form", f"form {mask(acct)}, document {shown or 'not found'}")
            ctx.issue("BANK_DOC_ACCOUNT_MISMATCH", "documents", "vendor", "Bank document shows a different account",
                      f"Form account {mask(acct)}; {fname} shows {shown or 'no matching account'}.",
                      request=f"The account number on '{fname}' doesn't match the one on your form ({mask(acct)}). Please confirm which account we should pay and send a bank letter for it.",
                      why="We only pay into an account confirmed by a bank-issued document.")
        for key, val in (("routing", s["routing_number"]), ("ifsc", s["ifsc"]), ("sort_code", s["sort_code"])):
            if val:
                found = D.alnum(val) in D.alnum(text)
                chk("pass" if found else "warn", f"{fname}: {key.replace('_', ' ')} matches", f"{val}" + ("" if found else " not found on document"))
        holder = f.get("holder")
        if holder:
            ctx.facts["doc_holders"].append((fname, holder))
            chk("info", f"{fname}: account holder on document", holder)
        else:
            sim, line = text_has_name(s["legal_name"], text)
            if sim >= 0.85:
                ctx.facts["doc_holders"].append((fname, s["legal_name"]))
                chk("pass", f"{fname}: legal name printed on the bank document", f"'{line}' ({round(sim * 100)}%)")
            else:
                chk("warn", f"{fname}: account holder shown on document", "no holder name found - cannot cross-check")
    elif dtype == "insurance":
        exp = D.parse_date(f.get("expiry"))
        if not exp:
            chk("fail", f"{fname}: expiry date readable", f"found '{f.get('expiry')}'")
            ctx.issue("INSURANCE_NO_EXPIRY", "documents", "vendor", "Insurance expiry date not found", f"{fname} has no readable expiry date.",
                      request="We couldn't find an expiry date on your insurance certificate. Please send a certificate showing the policy period.",
                      why=_doc_why("insurance"))
        elif exp < today:
            chk("fail", f"{fname}: insurance is current", f"expired on {exp:%d %b %Y} ({(today - exp).days} days ago)")
            ctx.issue("INSURANCE_EXPIRED", "documents", "vendor", "Insurance certificate expired",
                      f"Policy expired {exp:%d %b %Y}.",
                      request=f"Your certificate of insurance expired on {exp:%d %B %Y}. Please send the current (renewed) certificate.",
                      why=_doc_why("insurance"))
        elif (exp - today).days <= INSURANCE_WARN_DAYS:
            chk("warn", f"{fname}: insurance is current", f"expires {exp:%d %b %Y} - in {(exp - today).days} days; renewal reminder set")
        else:
            chk("pass", f"{fname}: insurance is current", f"valid until {exp:%d %b %Y}")
    elif dtype == "incorporation":
        reg = D.alnum(s["registration_number"])
        if reg:
            found = reg in D.alnum(text)
            chk("pass" if found else "fail", f"{fname}: registration number matches the form",
                f"{s['registration_number']}" + ("" if found else f" not on document (document shows {f.get('reg_no') or '?'})"))
            if not found:
                ctx.issue("REGNO_MISMATCH", "documents", "vendor", "Registration number doesn't match the certificate",
                          f"Form {s['registration_number']}; certificate {f.get('reg_no') or 'not found'}.",
                          request=f"The company registration number on your form ({s['registration_number']}) doesn't match your certificate "
                                  f"({f.get('reg_no') or 'not shown'}). Please confirm the correct number.",
                          why="It identifies the legal entity we are contracting with.")
        inc = D.parse_date(f.get("inc_date"))
        if inc:
            age = (today - inc).days
            ctx.facts["entity_age_days"] = age
            if age < NEW_ENTITY_DAYS:
                chk("warn", f"{fname}: company age", f"incorporated {inc:%d %b %Y} - only {age} days ago")
                ctx.facts["risk_signals"].append((f"Company incorporated only {age} days ago", 15))
            else:
                chk("pass", f"{fname}: company age", f"incorporated {inc:%d %b %Y} ({age // 365} years)")
        if country == "IN" and re.fullmatch(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}", reg or ""):
            st_code = reg[6:8]
            st_name = V.CIN_STATES.get(st_code)
            ok = bool(st_name) and V.normalize_name(st_name) == V.normalize_name(s["state"])
            chk("pass" if ok else "warn", "CIN registration state matches the address", f"CIN state {st_code} = {st_name or '?'}; address state {s['state']}")


def _holder_check(ctx: Ctx, chk):
    s = ctx.sub
    legal, trading = s["legal_name"], s["trading_name"]
    sources = [("form", s["account_holder"])] + [(f"bank document {fn}", h) for fn, h in ctx.facts["doc_holders"]]
    sources = [(src, h) for src, h in sources if h]
    if not sources or not legal:
        chk("na", "Account holder matches legal entity", "nothing to compare")
        return
    worst = None
    for src, h in sources:
        sim = V.name_similarity(h, legal)
        st_ = "pass" if sim >= 0.9 else "fail"
        if st_ == "pass":
            nh, nl = V.normalize_name(h), V.normalize_name(legal)
            detail = (f"'{h}' vs '{legal}': identical after removing legal-form words ('{nl}')" if nh == nl and h.lower() != legal.lower()
                      else f"'{h}' vs '{legal}' ({round(sim * 100)}%)")
            chk("pass", f"Account holder ({src}) matches legal entity", detail)
            continue
        if trading and V.name_similarity(h, trading) >= 0.9:
            chk("warn", f"Account holder ({src}) matches legal entity", f"'{h}' is the trading name, not the legal name - acceptable, noted")
            continue
        chk("fail", f"Account holder ({src}) matches legal entity", f"'{h}' vs '{legal}' ({round(sim * 100)}%)")
        if worst is None or sim < worst[2]:
            worst = (src, h, sim)
    if not worst:
        return
    src, h, sim = worst
    form_h = s["account_holder"]
    hidden = src != "form" and V.name_similarity(form_h, legal) >= 0.9
    personal = bool(s["contact_name"] and V.name_similarity(h, s["contact_name"]) >= 0.85) or V.looks_personal(h)
    if personal:
        ctx.issue("BANK_HOLDER_PERSONAL", "bank", "vendor", "Bank account appears to be a personal account",
                  f"Account holder '{h}' (from {src}) looks like an individual, not '{legal}'.",
                  request=f"The bank account is held by '{h}', which appears to be a personal account. We can only pay {legal} into a business "
                          f"account in the company's name. Please provide a bank letter or cancelled cheque for an account held by {legal}.",
                  why="Paying a company invoice into a personal account is a common fraud pattern and creates tax problems for both sides.",
                  internal="Personal account offered for a company vendor.")
    else:
        ctx.issue("BANK_HOLDER_MISMATCH", "bank", "vendor", "Bank account is held by a different company",
                  f"Account holder '{h}' (from {src}) is not the legal entity '{legal}'" + (f"; the form claimed '{form_h}'" if hidden else "") + ".",
                  request=(f"Your bank letter shows the account is held by '{h}', but the company being registered is '{legal}'"
                           + (" (the form listed the holder as the registering company)" if hidden else "") +
                           f". We can only pay into an account held by the contracting legal entity. Please either send a bank letter for an account "
                           f"in the name of {legal}, or - if payments should go to a parent or group company - let us know and we'll send our "
                           "third-party payment authorisation form."),
                  why="Paying a different legal entity than the one we contract with is the most common route for invoice fraud.",
                  internal=("The form and the bank letter disagree on the account holder - only visible by cross-referencing the document. "
                            if hidden else "") + f"Holder name similarity to legal name {round(sim * 100)}%.")


def _reviewer_notes(ctx: Ctx) -> list[str]:
    notes = []
    for i in ctx.issues:
        notes.append(f"[{i['severity'].upper()}] {i['title']}: {i['detail']}" + (f" -> {i['internal']}" if i.get("internal") else ""))
    for c in ctx.checks:
        if c["status"] == "warn" and not any(c["name"] in n for n in notes):
            notes.append(f"[NOTE] {c['name']}: {c['detail']}")
    if not notes:
        notes.append("No exceptions. All checks passed.")
    return notes


def _vendor_email(decision: str, s: dict, ven: list[dict], vendor_id: str | None, today: date) -> tuple[str, str]:
    first = (s["contact_name"] or "there").split()[0]
    ref = s["submission_id"]
    sign = f"\n\nKind regards,\nSupplier Onboarding Team\n{BUYER}\nsupplier-onboarding@acme-industries.example"
    if decision == "APPROVED":
        subj = f"You're set up as a supplier to {BUYER} ({vendor_id})"
        body = (f"Dear {first},\n\nGood news - {s['legal_name']} has been approved as a supplier to {BUYER}.\n\n"
                f"Supplier ID: {vendor_id}\nPayments to: account ending {mask(s['iban'] or s['account_number'])[-4:]} ({s['bank_name'] or s['bank_country']})\n"
                f"Reference: {ref}\n\nPlease quote your supplier ID on every invoice.\n\n"
                "One important note: we will never change your bank details based on an email alone. If your details change, "
                "we will confirm by calling the number we already hold for you." + sign)
        return subj, body
    if decision == "REJECTED":
        subj = f"Your supplier registration with {BUYER} ({ref})"
        body = (f"Dear {first},\n\nThank you for your interest in working with {BUYER}. After reviewing the registration for "
                f"{s['legal_name']}, we're unable to proceed with setting you up as a supplier at this time.\n\n"
                f"If you believe this is in error, please contact the {BUYER} colleague who invited you to register, quoting reference {ref}." + sign)
        return subj, body
    deadline = today + timedelta(days=REPLY_DAYS)
    items = "\n\n".join(f"{n}. {i['request']}\n   Why we need this: {i['why']}" for n, i in enumerate(ven, 1))
    if decision == "PENDING":
        subj = f"Action needed: {len(ven)} item{'s' if len(ven) > 1 else ''} to complete your supplier registration ({ref})"
        body = (f"Dear {first},\n\nThank you for submitting {s['legal_name']}'s supplier registration. We've reviewed everything and "
                f"we're nearly there - we just need the following before we can set you up for payment:\n\n{items}\n\n"
                f"Everything else in your submission checked out, so we'll only re-check the items above. Please reply to this email "
                f"with the corrected details or documents, quoting reference {ref}, by {deadline:%d %B %Y}." + sign)
        return subj, body
    # MANUAL_REVIEW: neutral holding message; never reveal screening details
    subj = f"Your supplier registration is being reviewed ({ref})"
    body = (f"Dear {first},\n\nThank you for submitting {s['legal_name']}'s supplier registration. It is now going through a routine "
            f"additional check by our compliance team, which normally takes up to 2 business days. ")
    if ven:
        body += f"In the meantime, it would help if you could send us the following:\n\n{items}\n\nPlease reply quoting reference {ref}."
    else:
        body += "There's nothing you need to do right now - we'll be in touch as soon as it's complete."
    return subj, body + sign


def _polish(subject: str, body: str, decision: str, st) -> tuple[str, str]:
    """Optional: let Claude make the email read more naturally, keeping every fact. Rules template otherwise."""
    if not llm.available():
        st.log("Email drafted from rules template (no LLM configured)")
        return body, "rules template"
    try:
        out = llm.complete_json(
            "You edit supplier-onboarding emails for a procurement team. Keep every requested item, number, date, reference and "
            "name exactly. Keep it polite, plain-English and concise. Never add reasons or details that are not in the draft "
            "(especially for rejections or compliance reviews).",
            f"Decision: {decision}\nSubject: {subject}\n\nDraft:\n{body}\n\nReturn the improved body only.",
            {"type": "object", "properties": {"body": {"type": "string"}}},
        )
        st.log(f"Email polished by {llm.engine_name()}", "ok")
        return out["body"], llm.engine_name()
    except Exception as e:  # noqa: BLE001
        st.log(f"LLM polish failed ({type(e).__name__}); using rules template", "warn")
        return body, "rules template (LLM fallback)"


def _llm_fill(path: Path, dtype: str, fields: dict, st) -> dict:
    keys = {"tax_document": ["name", "tax_id"], "bank_proof": ["holder", "account", "iban"], "insurance": ["name", "expiry"],
            "incorporation": ["name", "reg_no", "inc_date"]}[dtype]
    missing = [k for k in keys if not fields.get(k)]
    if not missing or path.suffix.lower() != ".pdf":
        return fields
    try:
        out = llm.complete_json("Extract fields from a vendor onboarding document. Use empty string when absent. Dates as YYYY-MM-DD.",
                                f"Document type: {dtype}. Extract: {', '.join(missing)}.",
                                {"type": "object", "properties": {k: {"type": "string"} for k in missing}}, pdf_bytes=path.read_bytes())
        filled = {k: v for k, v in out.items() if v}
        st.log(f"Claude filled {list(filled)} on {path.name}", "ok")
        return fields | filled
    except Exception as e:  # noqa: BLE001
        st.log(f"Claude extraction failed on {path.name} ({type(e).__name__}); rules only", "warn")
        return fields


def _write_eml(path: Path, to: str, subject: str, body: str) -> dict:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = FROM_ADDR, to, subject
    m["Date"] = format_datetime(datetime.now().astimezone())
    m.set_content(body, charset="utf-8", cte="8bit")
    path.write_bytes(bytes(m))
    return {"to": to, "from": FROM_ADDR, "subject": subject, "body": body, "path": _rel(path)}


APPROVED_COLS = ["vendor_id", "legal_name", "trading_name", "country", "tax_id", "registration_number", "account_holder", "bank_name",
                 "bank_country", "currency", "iban", "account_number", "routing_number", "ifsc", "sort_code", "swift", "contact_name",
                 "contact_email", "status", "onboarded_on", "root_submission_id", "submission_id", "run_id"]


def _append_approved(s: dict, root: str, run_id: str) -> str:
    """Create the vendor record. Idempotent per onboarding case: a case that already has a record gets that ID back."""
    with _WRITE_LOCK:
        existing = find_approved(root)
        if existing:
            return existing["vendor_id"]
        rows = []
        if APPROVED_CSV.exists():
            with APPROVED_CSV.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        nums = [int(r["vendor_id"].split("-")[1]) for r in rows if r.get("vendor_id", "").startswith("V-")]
        vid = f"V-{max(nums + [2000]) + 1}"
        exists = APPROVED_CSV.exists()
        with APPROVED_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=APPROVED_COLS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow({**s, "vendor_id": vid, "status": "active", "onboarded_on": date.today().isoformat(),
                        "root_submission_id": root, "run_id": run_id})
        return vid


def find_approved(root: str) -> dict | None:
    """The vendor record already created for this onboarding case, if any."""
    if not root or not APPROVED_CSV.exists():
        return None
    with APPROVED_CSV.open(newline="", encoding="utf-8") as f:
        return next((r for r in csv.DictReader(f) if r.get("root_submission_id") == root), None)


def audit(event: dict) -> None:
    """Append one line to the audit log. Never rewritten: every automated and human decision is kept."""
    with _WRITE_LOCK:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _utc_now(), **event}, default=str) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(APP_DIR.parent)).replace("\\", "/")
    except ValueError:
        return str(p)


def load_scenario(sid: str) -> dict:
    sc = next((x for x in SCENARIOS if x["id"] == sid), None)
    if not sc:
        raise KeyError(sid)
    return json.loads((SAMPLES / f"{sid}.json").read_text(encoding="utf-8"))
