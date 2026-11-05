"""PS-1 · Invoice processing — from PDF to a reasoned AP decision.

Stages (each visible live in the UI):
  intake -> read -> extract -> vendor -> validate -> po -> lines -> duplicate -> decide -> act

Decisions:
  APPROVE           all controls pass (variances inside tolerance) -> posted to ledger, payment scheduled
  REVIEW            a human must look (price/qty exception, over-billing, inferred PO, bank-detail change ...)
                    -> routed to the PO's buyer with a specific question
  RETURN_TO_VENDOR  invoice can't be processed as sent (missing invoice no./date, arithmetic broken)
                    -> email drafted to the vendor's address *from the vendor master*, not the invoice
  REJECT            must not be paid (duplicate of a paid invoice, blocked vendor, unknown vendor)
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from common import llm
import extract
from extract import extract_llm, extract_rules, norm, parse_date, read_document

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUTPUTS = HERE / "outputs"
OUTBOX = HERE / "outbox"
POSTED = DATA / "ledger_posted.csv"
extract.OCR_CACHE = DATA / "ocr_cache"

# ---- policy (the knobs an AP lead would own) ---------------------------------
POLICY = {
    "price_tolerance_pct": 2.0,        # unit price may exceed PO by up to 2% ...
    "price_tolerance_abs": 50.0,       # ... and the total price variance must be <= 50 in invoice currency
    "po_overbill_tolerance_pct": 2.0,  # cumulative billing may exceed PO value only by the price tolerance
    "arith_tolerance": 0.05,           # rounding allowance when re-adding the invoice
    "max_invoice_age_days": 180,
    "dup_window_days": 10,
    "vendor_match_min": 0.80,
    "po_inference_min": 0.75,
}
TODAY = date.today()

PLAN = [
    ("intake", "Receive invoice"),
    ("read", "Read document"),
    ("extract", "Extract fields"),
    ("vendor", "Identify vendor"),
    ("validate", "Validate invoice"),
    ("po", "Match purchase order"),
    ("lines", "3-way match: PO / invoice / receipt"),
    ("duplicate", "Duplicate check"),
    ("decide", "Decide"),
    ("act", "Take action"),
]

SEVERITY_RANK = {"reject": 4, "hold": 3, "return": 2, "review": 1}
DECISION_FOR = {"reject": "REJECT", "hold": "REVIEW", "return": "RETURN_TO_VENDOR", "review": "REVIEW"}


# ----------------------------------------------------------------------------- reference data
def _csv(name: str) -> list[dict]:
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_reference() -> dict:
    vendors = _csv("vendors.csv")
    pos = {p["po_number"]: dict(p, total_amount=float(p["total_amount"])) for p in _csv("purchase_orders.csv")}
    lines: dict[str, list] = {}
    for l in _csv("po_lines.csv"):
        lines.setdefault(l["po_number"], []).append(dict(l, qty=float(l["qty"]), unit_price=float(l["unit_price"])))
    grn: dict[tuple, float] = {}
    for g in _csv("goods_receipts.csv"):
        k = (g["po_number"], g["sku"])
        grn[k] = grn.get(k, 0) + float(g["qty_received"])
    return {"vendors": vendors, "pos": pos, "po_lines": lines, "grn": grn, "ledger": load_ledger()}


LEDGER_FIELDS = ["vendor_id", "invoice_number", "invoice_date", "po_number", "currency", "net_amount", "total_amount",
                 "status", "file_hash", "posted_at", "line_qty", "source"]


def load_ledger() -> list[dict]:
    rows = _csv("invoice_ledger_seed.csv")
    if POSTED.exists():
        rows += _csv(POSTED.name)
    for r in rows:
        r["net_amount"] = float(r["net_amount"] or 0)
        r["total_amount"] = float(r["total_amount"] or 0)
        r["line_qty_map"] = {k: float(v) for k, v in (x.split(":") for x in r.get("line_qty", "").split("|") if ":" in x)}
    return rows


def reset_ledger():
    if POSTED.exists():
        POSTED.unlink()


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ----------------------------------------------------------------------------- pipeline
class Issues(list):
    def add(self, severity: str, code: str, message: str, stage: str):
        self.append({"severity": severity, "code": code, "message": message, "stage": stage})


def run_pipeline(run, inp: dict) -> dict:
    ref = load_reference()
    issues = Issues()
    checks: list[dict] = []  # flat list of control results for the UI
    pdf_bytes = Path(inp["path"]).read_bytes()
    filename = inp.get("filename") or Path(inp["path"]).name

    def check(stage, name, status, detail):
        checks.append({"stage": stage, "name": name, "status": status, "detail": detail})

    # ---------------------------------------------------------------- intake
    with run.stage("intake") as st:
        file_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]
        st.log(f"Received '{filename}' ({len(pdf_bytes):,} bytes) via {inp.get('channel', 'upload')}")
        st.log(f"Fingerprint sha256:{file_hash} — used later to catch re-sent files")
        st.data(file=filename, sha256=file_hash, received=datetime.now().isoformat(timespec="seconds"))

    # ---------------------------------------------------------------- read
    with run.stage("read") as st:
        doc = read_document(pdf_bytes, filename, log=st.log)
        st.data(method=doc["method"], pages=doc["pages"], characters=len(doc["text"]), ocr_confidence=doc["ocr_confidence"])
        if doc["method"] == "text-layer":
            st.log("PDF has a machine-readable text layer — reading it directly", "ok")
        else:
            st.log("No text layer found — this is a scanned image. Rendered the page and ran OCR.", "warn")
            st.log(f"Mean OCR confidence {doc['ocr_confidence']:.1%}")
            st.note(f"Scanned document - OCR used (confidence {doc['ocr_confidence']:.0%})")
            if (doc["ocr_confidence"] or 0) < 0.85:
                issues.add("review", "LOW_OCR", f"OCR confidence is low ({doc['ocr_confidence']:.0%}); a human should verify the figures.", "read")
            st.status("warn")
        st.data(text_preview=doc["text"][:1500])
        if len(doc["text"].strip()) < 20:
            issues.add("return", "UNREADABLE", "The document could not be read (no text found even after OCR).", "read")
            st.status("fail", "Unreadable document")

    # ---------------------------------------------------------------- extract
    with run.stage("extract") as st:
        engine = "rules"
        fields = None
        if llm.available():
            try:
                st.log(f"Asking {llm.MODEL} to read the document into a fixed schema…")
                fields = extract_llm(pdf_bytes)
                engine = "claude"
                st.log("Claude extraction succeeded", "ok")
            except Exception as e:
                st.log(f"Claude extraction failed ({e}); falling back to rules extractor", "warn")
        if fields is None:
            st.log("Using deterministic extractor (label patterns + table detection)")
            fields = extract_rules(doc["text"], ocr=doc["method"] != "text-layer")
        v = lambda k: (fields.get(k) or {}).get("value")  # noqa: E731
        summary = {
            "invoice_number": v("invoice_number"), "invoice_date (as printed)": v("invoice_date_raw"),
            "po_references": [p["value"] for p in fields["po_references"]], "currency": v("currency"),
            "subtotal": v("subtotal"), "tax": v("tax_amount"), "total": v("total"),
            "tax_inclusive": fields["tax_inclusive"], "line_items": len(fields["line_items"]),
            "vendor_tax_id": v("vendor_tax_id"), "bank_account": v("bank_account"),
        }
        st.data(engine=engine, **summary)
        for k in ("invoice_number", "invoice_date_raw", "total"):
            if v(k) is None:
                st.log(f"Could not find: {k.replace('_raw', '')}", "warn")
        found = sum(1 for k in ("invoice_number", "invoice_date_raw", "total", "currency", "vendor_tax_id") if v(k) is not None)
        st.log(f"Captured {found}/5 header fields and {len(fields['line_items'])} line item(s)")
        if any(li.get("bundled") for li in fields["line_items"]):
            st.log("Line items are bundled (single amount, no qty/price breakdown)", "warn")

    # ---------------------------------------------------------------- vendor
    vendor = None
    with run.stage("vendor") as st:
        text = doc["text"]
        head = "\n".join(text.splitlines()[:8])
        scored = []
        for vd in ref["vendors"]:
            names = [vd["name"]] + [a for a in vd["aliases"].split("|") if a]
            name_score = max(
                max((1.0 if norm(n) and norm(n) in norm(head) else sim(n, head_line)) for head_line in head.splitlines() or [""])
                for n in names
            )
            tax_match = bool(v("vendor_tax_id")) and norm(v("vendor_tax_id")) == norm(vd["tax_id"])
            email_match = any(e.split("@")[-1] == vd["remit_email"].split("@")[-1] for e in fields["emails"])
            score = max(name_score, 1.0 if tax_match else 0) * (1.0 if not v("vendor_tax_id") or tax_match else 0.85)
            if email_match:
                score = min(1.0, score + 0.05)
            scored.append({"vendor_id": vd["vendor_id"], "name": vd["name"], "name_score": round(name_score, 2),
                           "tax_id_match": tax_match, "email_domain_match": email_match, "score": round(score, 2)})
        scored.sort(key=lambda s: -s["score"])
        st.data(candidates=scored[:3])
        best = scored[0]
        if best["score"] >= POLICY["vendor_match_min"]:
            vendor = next(x for x in ref["vendors"] if x["vendor_id"] == best["vendor_id"])
            how = ", ".join(k for k, ok in [("tax ID", best["tax_id_match"]), ("name", best["name_score"] >= 0.8), ("email domain", best["email_domain_match"])] if ok)
            st.log(f"Matched vendor master {vendor['vendor_id']} · {vendor['name']} (score {best['score']}, via {how})", "ok")
            check("vendor", "Vendor in master", "pass", f"{vendor['name']} ({vendor['vendor_id']}) matched via {how}")
            # tax ID on the invoice contradicts the master -> lookalike vendor risk
            if v("vendor_tax_id") and not best["tax_id_match"]:
                issues.add("hold", "TAXID_MISMATCH", f"Invoice tax ID {v('vendor_tax_id')} does not match the {vendor['name']} master record ({vendor['tax_id']}). Possible lookalike vendor.", "vendor")
                check("vendor", "Tax ID matches master", "fail", f"{v('vendor_tax_id')} vs {vendor['tax_id']}")
            if vendor["status"] != "approved":
                issues.add("reject", "VENDOR_BLOCKED", f"Vendor {vendor['name']} is '{vendor['status']}' in the vendor master and cannot be paid.", "vendor")
                check("vendor", "Vendor approved for payment", "fail", f"Status: {vendor['status']}")
                st.status("fail", f"Vendor status: {vendor['status']}")
            else:
                check("vendor", "Vendor approved for payment", "pass", f"Status approved, terms Net {vendor['payment_terms_days']}")
            # bank details — the #1 AP fraud vector
            bank = v("bank_account")
            if bank and bank != vendor["bank_account"]:
                issues.add("hold", "BANK_CHANGED", f"Remit-to account on the invoice ({bank}) differs from the account on file ({vendor['bank_account']}). Do not pay until verified by phone with a known contact.", "vendor")
                check("vendor", "Bank details match master", "fail", f"Invoice {bank} vs master {vendor['bank_account']}")
                st.log("Remit-to bank account differs from master — payment hold", "error")
                st.status("warn", "Bank details changed")
            elif bank:
                check("vendor", "Bank details match master", "pass", f"Account {bank} matches")
            else:
                check("vendor", "Bank details match master", "na", "No bank details on invoice; master account will be used")
            st.data(vendor_id=vendor["vendor_id"], vendor=vendor["name"], country=vendor["country"], currency=vendor["currency"], terms_days=vendor["payment_terms_days"])
        else:
            st.log(f"No vendor-master record matches (best: {best['name']} @ {best['score']})", "error")
            issues.add("reject", "UNKNOWN_VENDOR", "Sender is not an approved vendor. Invoices from unknown vendors are not paid; route to vendor onboarding if legitimate.", "vendor")
            check("vendor", "Vendor in master", "fail", f"Best candidate {best['name']} scored {best['score']}")
            st.status("fail", "Unknown vendor")

    # ---------------------------------------------------------------- validate
    inv_date = None
    with run.stage("validate") as st:
        cur = v("currency") or (vendor or {}).get("currency")
        # required fields
        for key, label in [("invoice_number", "Invoice number"), ("invoice_date_raw", "Invoice date"), ("total", "Invoice total")]:
            if v(key) is None:
                issues.add("return", f"MISSING_{key.upper()}", f"{label} is missing from the invoice.", "validate")
                check("validate", f"{label} present", "fail", "Not found on the document")
            else:
                check("validate", f"{label} present", "pass", str(v(key)))
        # currency vs vendor
        if vendor and cur and cur != vendor["currency"]:
            issues.add("review", "CURRENCY", f"Invoice currency {cur} differs from vendor's contracted currency {vendor['currency']}.", "validate")
            check("validate", "Currency", "fail", f"{cur} vs {vendor['currency']}")
        elif cur:
            check("validate", "Currency", "pass", cur)
        # date: interpret numeric dates in the vendor's locale (UK = day-first)
        if v("invoice_date_raw"):
            day_first = (vendor or {}).get("country") not in (None, "US")
            inv_date, ambiguous = parse_date(v("invoice_date_raw"), day_first)
            if inv_date is None:
                issues.add("return", "BAD_DATE", f"Invoice date '{v('invoice_date_raw')}' could not be understood.", "validate")
                check("validate", "Invoice date valid", "fail", v("invoice_date_raw"))
            else:
                note = f"{v('invoice_date_raw')} → {inv_date.isoformat()}"
                if ambiguous:
                    note += f" (ambiguous; read {'day-first' if day_first else 'month-first'} per vendor country {vendor['country'] if vendor else '?'})"
                    st.log("Date is numerically ambiguous — resolved using the vendor's country convention", "warn")
                if inv_date > TODAY:
                    issues.add("return", "FUTURE_DATE", f"Invoice is dated in the future ({inv_date}).", "validate")
                    check("validate", "Invoice date valid", "fail", note + " - in the future")
                elif (TODAY - inv_date).days > POLICY["max_invoice_age_days"]:
                    issues.add("review", "STALE", f"Invoice is {(TODAY - inv_date).days} days old.", "validate")
                    check("validate", "Invoice date valid", "warn", note + " - older than policy")
                else:
                    check("validate", "Invoice date valid", "pass", note)
                st.data(invoice_date=inv_date.isoformat())

        # arithmetic — re-add the invoice ourselves
        items = fields["line_items"]
        tol = POLICY["arith_tolerance"]
        line_sum = round(sum(li["amount"] for li in items), 2)
        bad_lines = [li for li in items if abs(li["qty"] * li["unit_price"] - li["amount"]) > tol]
        if bad_lines:
            issues.add("return", "LINE_MATH", f"{len(bad_lines)} line(s) where qty × price ≠ amount.", "validate")
            check("validate", "Line arithmetic", "fail", "; ".join(li["description"] for li in bad_lines))
        elif items:
            check("validate", "Line arithmetic", "pass", f"{len(items)} lines re-multiplied OK")
        total, sub, tax = v("total"), v("subtotal"), v("tax_amount")
        rate = fields.get("tax_rate_stated")
        if fields["tax_inclusive"]:
            # prices include tax: lines should add to the gross total; derive net with the stated rate
            st.log(f"Prices are tax-inclusive (rate stated: {rate}%) — converting to net before comparing with the PO")
            net = round(total / (1 + (rate or 0) / 100), 2) if total is not None else None
            tax_calc = round(total - net, 2) if net is not None else None
            if total is not None and items and abs(line_sum - total) > tol:
                issues.add("return", "TOTAL_MISMATCH", f"Lines add to {line_sum:,.2f} but total says {total:,.2f}.", "validate")
                check("validate", "Lines add up to total", "fail", f"{line_sum:,.2f} vs {total:,.2f}")
            elif items:
                check("validate", "Lines add up to total", "pass", f"{line_sum:,.2f} = gross total")
            if tax is not None and tax_calc is not None and abs(tax - tax_calc) > tol:
                issues.add("review", "TAX_MISMATCH", f"Stated tax {tax:,.2f} ≠ {rate}% of gross ({tax_calc:,.2f}).", "validate")
                check("validate", "Embedded tax consistent", "fail", f"{tax} vs {tax_calc}")
            elif tax_calc is not None:
                check("validate", "Embedded tax consistent", "pass", f"Net {net:,.2f} + tax {tax_calc:,.2f} = {total:,.2f}")
            tax = tax_calc
        else:
            net = sub if sub is not None else (line_sum if items else (total - (tax or 0) if total is not None else None))
            if sub is not None and items and abs(line_sum - sub) > tol:
                issues.add("return", "SUBTOTAL_MISMATCH", f"Lines add to {line_sum:,.2f} but subtotal says {sub:,.2f}.", "validate")
                check("validate", "Lines add up to subtotal", "fail", f"{line_sum:,.2f} vs {sub:,.2f}")
            elif items:
                check("validate", "Lines add up to subtotal", "pass", f"{line_sum:,.2f}")
            if total is not None and net is not None and abs(net + (tax or 0) - total) > tol:
                issues.add("return", "TOTAL_MISMATCH", f"Net {net:,.2f} + tax {tax or 0:,.2f} ≠ total {total:,.2f}.", "validate")
                check("validate", "Net + tax = total", "fail", f"{net + (tax or 0):,.2f} vs {total:,.2f}")
            elif total is not None and net is not None:
                check("validate", "Net + tax = total", "pass", f"{net:,.2f} + {tax or 0:,.2f} = {total:,.2f}")
            if tax and net:
                eff = 100 * tax / net
                expected = rate if rate is not None else None
                if expected is not None and abs(eff - expected) > 0.05:
                    issues.add("review", "TAX_RATE", f"Tax is {eff:.2f}% of net but the invoice states {expected}%.", "validate")
                    check("validate", "Tax rate consistent", "fail", f"{eff:.2f}% vs {expected}%")
                else:
                    check("validate", "Tax rate consistent", "pass", f"{eff:.2f}% of net")
        st.data(net_amount=net, tax=tax, gross_total=total, line_sum=line_sum, currency=cur)
        if any(i["stage"] == "validate" and i["severity"] in ("return", "reject") for i in issues):
            st.status("warn", "Invoice is incomplete or inconsistent")

    # ---------------------------------------------------------------- PO match
    po = None
    po_inferred = False
    with run.stage("po") as st:
        refs = [p["value"] for p in fields["po_references"]]
        if not vendor:
            st.status("skipped", "No vendor - PO match not attempted")
        elif refs:
            st.log(f"PO reference(s) printed on invoice: {', '.join(refs)} (normalised from vendor formatting)")
            for r in refs:
                p = ref["pos"].get(r)
                if not p:
                    st.log(f"{r} does not exist in the procurement system", "warn")
                    continue
                if p["vendor_id"] != vendor["vendor_id"]:
                    issues.add("hold", "PO_OTHER_VENDOR", f"{r} belongs to a different vendor ({p['vendor_id']}).", "po")
                    st.log(f"{r} belongs to vendor {p['vendor_id']}, not {vendor['vendor_id']}", "error")
                    continue
                po = p
                break
            if not po:
                issues.add("review", "PO_NOT_FOUND", f"PO reference(s) {', '.join(refs)} not found for this vendor.", "po")
                check("po", "PO exists for vendor", "fail", ", ".join(refs))
                st.status("warn", "Referenced PO not found")
        else:
            # implied PO: score this vendor's POs on amount, description overlap and period tokens
            st.log("No PO number on the invoice — inferring from this vendor's purchase orders", "warn")
            cands = []
            inv_desc = " ".join(li["description"] for li in fields["line_items"]).lower()
            inv_periods = set(re.findall(r"\bq[1-4]\b|\b20\d{2}\b|january|february|march|april|may|june|july|august|september|october|november|december", inv_desc))
            for p in ref["pos"].values():
                if p["vendor_id"] != vendor["vendor_id"]:
                    continue
                pl = ref["po_lines"].get(p["po_number"], [])
                po_desc = (p["description"] + " " + " ".join(l["description"] for l in pl)).lower()
                amt = 1.0 if net is not None and abs(net - p["total_amount"]) < 0.01 else max(0.0, 1 - abs((net or 0) - p["total_amount"]) / max(p["total_amount"], 1))
                desc = max((sim(li["description"], l["description"]) for li in fields["line_items"] for l in pl), default=0)
                po_periods = set(re.findall(r"\bq[1-4]\b|\b20\d{2}\b", po_desc))
                period = 1.0 if inv_periods & po_periods and not (inv_periods & {"q1", "q2", "q3", "q4"}) - po_periods else (0.0 if inv_periods & {"q1", "q2", "q3", "q4"} else 0.5)
                status_ok = 1.0 if p["status"] == "open" else 0.3
                score = round((0.45 * amt + 0.25 * desc + 0.30 * period) * status_ok, 3)
                cands.append({"po": p["po_number"], "description": p["description"], "amount": p["total_amount"], "status": p["status"],
                              "amount_fit": round(amt, 2), "text_fit": round(desc, 2), "period_fit": period, "score": score})
            cands.sort(key=lambda c: -c["score"])
            st.data(candidates=cands)
            for c in cands:
                st.log(f"  {c['po']}  score {c['score']:.2f}  (amount {c['amount_fit']}, text {c['text_fit']}, period {c['period_fit']})")
            margin = cands[0]["score"] - (cands[1]["score"] if len(cands) > 1 else 0) if cands else 0
            if cands and cands[0]["score"] >= POLICY["po_inference_min"] and margin >= 0.15:
                po = ref["pos"][cands[0]["po"]]
                po_inferred = True
                st.log(f"Best match {po['po_number']} — clear winner (margin {margin:.2f}). Will require confirmation.", "ok")
                issues.add("review", "PO_INFERRED", f"No PO on invoice; inferred {po['po_number']} ({po['description']}). A buyer must confirm.", "po")
                check("po", "PO reference", "warn", f"Not on invoice - inferred {po['po_number']} (score {cands[0]['score']:.2f}, margin {margin:.2f})")
                st.status("warn", f"PO inferred: {po['po_number']}")
            else:
                issues.add("return", "NO_PO", "No PO reference, and no single purchase order clearly matches.", "po")
                check("po", "PO reference", "fail", "None on invoice and no confident match")
                st.status("warn", "No PO could be determined")
        if po:
            if not po_inferred:
                check("po", "PO exists for vendor", "pass", f"{po['po_number']} · {po['description']} · buyer {po['buyer']}")
            if po["status"] != "open":
                st.log(f"{po['po_number']} is {po['status']} — it has already been fully invoiced/closed", "warn")
                issues.add("review", "PO_CLOSED", f"{po['po_number']} is {po['status']}.", "po")
                check("po", "PO is open", "fail", po["status"])
            else:
                check("po", "PO is open", "pass", "open")
            if po["currency"] != (v("currency") or po["currency"]):
                issues.add("review", "PO_CURRENCY", f"PO currency {po['currency']} vs invoice {v('currency')}.", "po")
            prev = [l for l in ref["ledger"] if l["po_number"] == po["po_number"] and l["vendor_id"] == po["vendor_id"]]
            prev_net = round(sum(l["net_amount"] for l in prev), 2)
            st.data(po=po["po_number"], po_value=po["total_amount"], buyer=po["buyer"], previously_invoiced=prev_net,
                    previous_invoices=[f"{l['invoice_number']} ({l['net_amount']:,.2f})" for l in prev], inferred=po_inferred)
            if prev:
                st.log(f"{len(prev)} earlier invoice(s) already billed {prev_net:,.2f} of {po['total_amount']:,.2f} on this PO — treating as a split PO")

    # ---------------------------------------------------------------- 3-way line match
    line_rows = []
    billing = None
    with run.stage("lines") as st:
        if not po:
            st.status("skipped", "No PO to match against")
        else:
            pl = ref["po_lines"].get(po["po_number"], [])
            prev = [l for l in ref["ledger"] if l["po_number"] == po["po_number"] and l["vendor_id"] == po["vendor_id"]]
            prev_qty: dict[str, float] = {}
            for l in prev:
                for sku, q in l["line_qty_map"].items():
                    prev_qty[sku] = prev_qty.get(sku, 0) + q
            factor = 1 / (1 + (fields.get("tax_rate_stated") or 0) / 100) if fields["tax_inclusive"] else 1.0
            approvable = 0.0
            total_price_var = 0.0
            for li in fields["line_items"]:
                best, bscore = None, 0.0
                for l in pl:
                    s = 1.0 if li.get("sku") and li["sku"] == l["sku"] else sim(li["description"], l["description"])
                    if s > bscore:
                        best, bscore = l, s
                row = {"invoice_line": li["description"], "qty": li["qty"], "unit_price_net": round(li["unit_price"] * factor, 4)}
                if (not best or bscore < 0.6) and len(pl) == 1 and len(fields["line_items"]) == 1 and abs(li["amount"] * factor - pl[0]["qty"] * pl[0]["unit_price"]) < 0.01:
                    # bundled one-line invoice against a one-line PO with the same value
                    best, bscore = pl[0], 0.6
                    st.log(f"Bundled line mapped to the PO's only line ({pl[0]['sku']}) on value", "warn")
                if not best or bscore < 0.6:
                    row.update(status="fail", note="Not on the PO")
                    issues.add("review", "LINE_NOT_ON_PO", f"'{li['description']}' is not on {po['po_number']}.", "lines")
                    line_rows.append(row)
                    continue
                received = ref["grn"].get((po["po_number"], best["sku"]), 0)
                already = prev_qty.get(best["sku"], 0)
                unit_net = li["unit_price"] * factor
                var_pct = 100 * (unit_net - best["unit_price"]) / best["unit_price"] if best["unit_price"] else 0
                var_amt = (unit_net - best["unit_price"]) * li["qty"]
                total_price_var += var_amt
                row.update(sku=best["sku"], po_line=best["description"], match_score=round(bscore, 2), po_qty=best["qty"],
                           po_unit_price=best["unit_price"], previously_billed=already, received=received,
                           price_var_pct=round(var_pct, 2), price_var_amt=round(var_amt, 2))
                notes, status = [], "pass"
                if var_pct > POLICY["price_tolerance_pct"] + 1e-9:
                    status = "fail"
                    notes.append(f"price +{var_pct:.2f}% over PO")
                    issues.add("review", "PRICE_VARIANCE", f"{best['sku']}: unit price {unit_net:,.2f} is {var_pct:+.2f}% vs PO {best['unit_price']:,.2f} (tolerance {POLICY['price_tolerance_pct']}%).", "lines")
                elif abs(var_pct) > 0.005:
                    notes.append(f"price {var_pct:+.2f}% (within {POLICY['price_tolerance_pct']}% tolerance)")
                    if status == "pass":
                        status = "warn"
                cum = already + li["qty"]
                if cum > best["qty"] + 1e-9:
                    status = "fail"
                    notes.append(f"billed {cum:g} cumulatively vs {best['qty']:g} ordered")
                    issues.add("review", "QTY_OVER_PO", f"{best['sku']}: cumulative billed qty {cum:g} exceeds PO qty {best['qty']:g}.", "lines")
                if cum > received + 1e-9:
                    status = "fail"
                    notes.append(f"only {received:g} received ({already:g} already billed)")
                    issues.add("review", "QTY_OVER_RECEIPT", f"{best['sku']}: billing {li['qty']:g} more but only {max(received - already, 0):g} received and not yet billed.", "lines")
                approvable += max(0.0, min(li["qty"], received - already, best["qty"] - already)) * min(unit_net, best["unit_price"] * (1 + POLICY["price_tolerance_pct"] / 100))
                row.update(status=status, note="; ".join(notes) or "matches PO and receipt")
                line_rows.append(row)
                st.log(f"{best['sku']}: inv {li['qty']:g} × {unit_net:,.2f} | PO {best['qty']:g} × {best['unit_price']:,.2f} | recv {received:g} | prev billed {already:g} → {row['note']}",
                       "ok" if status == "pass" else ("warn" if status == "warn" else "error"))
            if abs(total_price_var) > POLICY["price_tolerance_abs"] and not any(i["code"] == "PRICE_VARIANCE" for i in issues):
                issues.add("review", "PRICE_VARIANCE_TOTAL", f"Total price variance {total_price_var:,.2f} exceeds the {POLICY['price_tolerance_abs']:,.0f} absolute tolerance.", "lines")
            prev_net = round(sum(l["net_amount"] for l in prev), 2)
            cum_net = round(prev_net + (net or 0), 2)
            billing = {"po_value": po["total_amount"], "previously_invoiced": prev_net, "this_invoice_net": net,
                       "cumulative": cum_net, "remaining_after": round(po["total_amount"] - cum_net, 2),
                       "pct_billed": round(100 * cum_net / po["total_amount"], 1) if po["total_amount"] else None,
                       "approvable_now": round(approvable, 2), "price_variance_total": round(total_price_var, 2)}
            if cum_net > po["total_amount"] * (1 + POLICY["po_overbill_tolerance_pct"] / 100) + POLICY["arith_tolerance"]:
                issues.add("review", "PO_OVERBILLED", f"Cumulative billing {cum_net:,.2f} would exceed PO value {po['total_amount']:,.2f} ({billing['pct_billed']}%).", "lines")
                check("lines", "Within PO value", "fail", f"{billing['pct_billed']}% of PO billed after this invoice")
            else:
                check("lines", "Within PO value", "pass", f"{billing['pct_billed']}% of PO billed after this invoice")
            fails = [r for r in line_rows if r["status"] == "fail"]
            check("lines", "Line items match PO & receipt", "fail" if fails else ("warn" if any(r["status"] == "warn" for r in line_rows) else "pass"),
                  f"{len(line_rows) - len(fails)}/{len(line_rows)} lines clean" + (f"; price variance {total_price_var:+,.2f} within tolerance" if not fails and total_price_var else ""))
            st.data(lines=line_rows, billing=billing)
            if fails:
                st.status("warn", f"{len(fails)} line exception(s)")

    # ---------------------------------------------------------------- duplicates
    dup_of = None
    with run.stage("duplicate") as st:
        inv_no = v("invoice_number")
        hits = []
        for l in ref["ledger"]:
            reasons = []
            if l["file_hash"] == file_hash:
                reasons.append("identical file")
            same_vendor = vendor and l["vendor_id"] == vendor["vendor_id"]
            if same_vendor and inv_no:
                a, b = norm(inv_no), norm(l["invoice_number"])
                if a == b:
                    reasons.append("same invoice number")
                elif (a.startswith(b) or b.startswith(a)) and re.fullmatch(r"(r|rev|copy|dup|duplicate|a|b|1|2|reprint|reminder)?", (a[len(b):] if a.startswith(b) else b[len(a):])):
                    reasons.append(f"invoice number '{inv_no}' is a variant of '{l['invoice_number']}'")
            if same_vendor and total is not None and abs(l["total_amount"] - total) < 0.01:
                reasons.append(f"same amount {total:,.2f}")
                if inv_date and l["invoice_date"]:
                    d = abs((inv_date - date.fromisoformat(l["invoice_date"])).days)
                    if d <= POLICY["dup_window_days"]:
                        reasons.append(f"date within {d} day(s)")
            if reasons:
                hits.append({"invoice": l["invoice_number"], "status": l["status"], "posted": l["posted_at"], "amount": l["total_amount"], "reasons": reasons})
        st.data(ledger_rows_checked=len(ref["ledger"]), hits=hits)
        strong = [h for h in hits if any(r in ("identical file", "same invoice number") or r.startswith("invoice number") for r in h["reasons"])
                  and (any(r.startswith("same amount") for r in h["reasons"]) or "identical file" in h["reasons"])]
        soft = [h for h in hits if h not in strong and len(h["reasons"]) >= 2]
        renumbered = [h for h in hits if h not in strong and any(r in ("same invoice number",) or r.startswith("invoice number") for r in h["reasons"])]
        if strong:
            dup_of = strong[0]
            st.log(f"DUPLICATE of {dup_of['invoice']} ({dup_of['status']} on {dup_of['posted']}): {', '.join(dup_of['reasons'])}", "error")
            issues.add("reject", "DUPLICATE", f"Duplicate of invoice {dup_of['invoice']} already {dup_of['status']} on {dup_of['posted']} ({', '.join(dup_of['reasons'])}).", "duplicate")
            check("duplicate", "Not previously processed", "fail", f"Matches {dup_of['invoice']}: {', '.join(dup_of['reasons'])}")
            st.status("fail", f"Duplicate of {dup_of['invoice']}")
        elif renumbered:
            h = renumbered[0]
            issues.add("review", "INVOICE_NO_REUSED", f"Invoice number matches {h['invoice']} but the amount differs - corrected re-bill or number reuse?", "duplicate")
            check("duplicate", "Not previously processed", "warn", f"Same number as {h['invoice']} with a different amount")
            st.status("warn", "Invoice number reused")
        elif soft:
            h = soft[0]
            issues.add("review", "POSSIBLE_DUPLICATE", f"Same vendor, amount and date as {h['invoice']} under a different number.", "duplicate")
            check("duplicate", "Not previously processed", "warn", f"Looks like {h['invoice']}: {', '.join(h['reasons'])}")
            st.status("warn", "Possible duplicate")
        else:
            st.log(f"Checked {len(ref['ledger'])} ledger entries (number variants, amount+date window, file fingerprint) — no match", "ok")
            check("duplicate", "Not previously processed", "pass" if inv_no else "warn",
                  "No match on number, amount/date or file fingerprint" if inv_no else "Checked amount/date and fingerprint only - no invoice number to compare")

    # ---------------------------------------------------------------- decide
    with run.stage("decide") as st:
        if issues:
            top = max(issues, key=lambda i: SEVERITY_RANK[i["severity"]])
            decision = DECISION_FOR[top["severity"]]
        else:
            decision = "APPROVE"
        by_sev = {s: [i for i in issues if i["severity"] == s] for s in SEVERITY_RANK}
        for i in sorted(issues, key=lambda i: -SEVERITY_RANK[i["severity"]]):
            st.log(f"[{i['severity'].upper()}] {i['message']}", "error" if i["severity"] in ("reject", "hold") else "warn")
        cur = v("currency") or (vendor or {}).get("currency") or ""
        amount_txt = f"{cur} {total:,.2f}" if total is not None else "unknown amount"
        if decision == "APPROVE":
            due = (inv_date or TODAY) + timedelta(days=int((vendor or {}).get("payment_terms_days") or v("payment_terms_days") or 30))
            notes = [c["detail"] for c in checks if c["status"] == "warn"]
            headline = f"Approved for payment — {amount_txt} to {vendor['name']}, due {due.isoformat()}."
            reason = "Every control passed" + (f" (noted: {'; '.join(notes)})" if notes else "") + "."
        elif decision == "REJECT":
            headline = f"Rejected — do not pay {amount_txt}."
            reason = " ".join(i["message"] for i in by_sev["reject"])
        elif decision == "RETURN_TO_VENDOR":
            headline = f"Returned to vendor — invoice can't be processed as sent."
            reason = " ".join(i["message"] for i in by_sev["return"])
        else:
            holds = by_sev["hold"]
            headline = ("Payment hold — fraud check required." if holds else f"Needs review — {len(by_sev['review'])} exception(s) for {po['buyer'] if po else 'AP'}.")
            reason = " ".join(i["message"] for i in (holds or by_sev["review"]))
        st.data(decision=decision, rule_hits=len(issues))
        st.log(f"Decision: {decision}", "ok" if decision == "APPROVE" else "warn")

    # ---------------------------------------------------------------- act
    actions = []
    email = None
    with run.stage("act") as st:
        OUTPUTS.mkdir(exist_ok=True)
        record = {
            "run_id": run.id, "decided_at": datetime.now().isoformat(timespec="seconds"), "decision": decision,
            "file": filename, "sha256": file_hash, "vendor_id": (vendor or {}).get("vendor_id"), "invoice_number": v("invoice_number"),
            "invoice_date": inv_date.isoformat() if inv_date else None, "po_number": (po or {}).get("po_number"),
            "currency": v("currency") or (vendor or {}).get("currency"), "net": net, "tax": tax, "total": total, "issues": issues,
        }
        if decision == "APPROVE":
            new = not POSTED.exists()
            with open(POSTED, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
                if new:
                    w.writeheader()
                w.writerow({"vendor_id": vendor["vendor_id"], "invoice_number": v("invoice_number"), "invoice_date": inv_date.isoformat() if inv_date else "",
                            "po_number": po["po_number"], "currency": record["currency"], "net_amount": net, "total_amount": total,
                            "status": "approved", "file_hash": file_hash, "posted_at": TODAY.isoformat(),
                            "line_qty": "|".join(f"{r['sku']}:{r['qty']:g}" for r in line_rows if r.get("sku")), "source": f"run {run.id}"})
            record["payment_due"] = due.isoformat()
            actions.append(f"Posted to AP ledger (data/ledger_posted.csv) and scheduled payment for {due.isoformat()}")
            st.log(actions[-1], "ok")
        elif decision == "REVIEW":
            owner = po["buyer"] if po and not by_sev["hold"] else "AP Controller (fraud desk)"
            record["assigned_to"] = owner
            actions.append(f"Created review task for {owner}")
            if billing and billing.get("approvable_now") and any(i["code"] in ("QTY_OVER_RECEIPT", "PO_OVERBILLED", "QTY_OVER_PO") for i in issues):
                actions.append(f"Suggested partial approval: {record['currency']} {billing['approvable_now']:,.2f} is backed by goods received; hold the rest")
            st.log("; ".join(actions), "warn")
        if decision in ("RETURN_TO_VENDOR", "REJECT") and vendor:
            email = draft_vendor_email(decision, vendor, fields, issues, po, dup_of, amount_txt, run.id)
            OUTBOX.mkdir(exist_ok=True)
            eml = OUTBOX / f"{run.id}_{vendor['vendor_id']}.eml"
            eml.write_text(f"To: {email['to']}\nSubject: {email['subject']}\nContent-Type: text/plain; charset=utf-8\n\n{email['body']}", encoding="utf-8")
            actions.append(f"Drafted vendor email to {email['to']} (outbox/{eml.name}) — address taken from vendor master, not the invoice")
            st.log(actions[-1], "ok")
        if decision == "REJECT":
            actions.append("Invoice blocked from payment run")
        out = OUTPUTS / f"{run.id}_decision.json"
        record["actions"] = actions
        out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        actions.append(f"Decision record written to outputs/{out.name}")
        st.data(actions=actions)

    return {
        "decision": decision,
        "summary": headline + " " + reason,
        "headline": headline,
        "reason": reason,
        "engine": engine,
        "vendor": {k: vendor[k] for k in ("vendor_id", "name", "country", "status", "payment_terms_days")} if vendor else None,
        "invoice": {"number": v("invoice_number"), "date": inv_date.isoformat() if inv_date else v("invoice_date_raw"),
                    "currency": v("currency") or (vendor or {}).get("currency"), "net": net, "tax": tax, "total": total,
                    "tax_inclusive": fields["tax_inclusive"], "read_method": doc["method"], "po": (po or {}).get("po_number"), "po_inferred": po_inferred},
        "fields": {k: fields[k] for k in ("invoice_number", "invoice_date_raw", "vendor_tax_id", "bank_account", "currency", "subtotal", "tax_amount", "total")},
        "line_items": fields["line_items"],
        "line_match": line_rows,
        "billing": billing,
        "checks": checks,
        "issues": sorted(issues, key=lambda i: -SEVERITY_RANK[i["severity"]]),
        "actions": actions,
        "email": email,
        "po_buyer": (po or {}).get("buyer"),
    }


def draft_vendor_email(decision, vendor, fields, issues, po, dup_of, amount_txt, run_id) -> dict:
    inv_no = (fields.get("invoice_number") or {}).get("value")
    ref_txt = f"invoice {inv_no}" if inv_no else f"your document for {amount_txt}"
    if decision == "REJECT" and dup_of:
        subject = f"Re: {inv_no or 'invoice'} — already processed as {dup_of['invoice']}"
        body = (f"Hello {vendor['name']} accounts team,\n\n"
                f"We received {ref_txt}. It matches invoice {dup_of['invoice']}, which was already {dup_of['status']} on {dup_of['posted']}, "
                f"so we will not process it again.\n\n"
                f"If you believe a payment is outstanding, please send a statement showing the open item and the payment reference you expected, "
                f"and we'll reconcile it with you.\n\nKind regards,\nAccounts Payable\n(ref {run_id})")
    elif decision == "REJECT":
        subject = f"Re: {inv_no or 'invoice'} — unable to process"
        body = (f"Hello,\n\nWe are unable to process {ref_txt}: " + " ".join(i["message"] for i in issues if i["severity"] == "reject") +
                f"\n\nPlease contact your purchasing contact.\n\nKind regards,\nAccounts Payable\n(ref {run_id})")
    else:
        needs = []
        for i in issues:
            if i["severity"] != "return":
                continue
            needs.append({
                "MISSING_INVOICE_NUMBER": "A unique invoice number (we can't post or de-duplicate without it)",
                "MISSING_INVOICE_DATE_RAW": "The invoice date",
                "MISSING_TOTAL": "A clear invoice total",
                "NO_PO": "Our purchase order number",
                "LINE_MATH": "Corrected line amounts (quantity × price doesn't equal the line total on some lines)",
                "SUBTOTAL_MISMATCH": "A corrected subtotal (line items don't add up to it)",
                "TOTAL_MISMATCH": "A corrected total (subtotal + tax doesn't equal the total)",
                "BAD_DATE": "A valid invoice date",
                "FUTURE_DATE": "A correct invoice date (the one shown is in the future)",
                "UNREADABLE": "A legible copy of the invoice (ideally a native PDF rather than a scan)",
            }.get(i["code"], i["message"]))
        if po and not (fields.get("po_references")):
            needs.append(f"Please quote our purchase order {po['po_number']} on the invoice")
        subject = f"Action needed: we can't process {ref_txt} yet"
        body = (f"Hello {vendor['name']} accounts team,\n\n"
                f"Thanks for sending {ref_txt}. Before we can schedule payment we need a corrected invoice with:\n\n" +
                "\n".join(f"  • {n}" for n in dict.fromkeys(needs)) +
                "\n\nPlease reply with a revised PDF and we'll process it straight away — nothing else is needed from you."
                f"\n\nKind regards,\nAccounts Payable\n(ref {run_id})")
    return {"to": vendor["remit_email"], "subject": subject, "body": body}
