"""Document reading + field extraction for invoices.

read_document():  text layer via pdfplumber; if a page has no text layer it is
                  rendered and OCR'd (rapidocr, offline). Images are OCR'd too.
extract_rules():  label-driven regex + table heuristics. Every extracted field
                  keeps the source line it came from ("evidence") so a reviewer
                  can see exactly why the process believes it.
extract_llm():    same output shape, produced by Claude reading the PDF directly
                  (used only when an API key is configured).
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

# ----------------------------------------------------------------------------- reading
_ocr_engine = None


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_image(pil_img) -> tuple[str, float]:
    """OCR one image; rebuild reading-order lines by grouping boxes on their vertical centre."""
    import numpy as np

    res, _ = _ocr()(np.array(pil_img.convert("RGB")))
    if not res:
        return "", 0.0
    items = []
    for box, txt, conf in res:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append({"y": sum(ys) / 4, "h": max(ys) - min(ys), "x": min(xs), "t": txt, "c": float(conf)})
    items.sort(key=lambda i: i["y"])
    lines, cur = [], []
    for it in items:
        if cur and abs(it["y"] - cur[-1]["y"]) > max(6, 0.55 * it["h"]):
            lines.append(cur)
            cur = []
        cur.append(it)
    if cur:
        lines.append(cur)
    text = "\n".join("  ".join(i["t"] for i in sorted(l, key=lambda i: i["x"])) for l in lines)
    conf = sum(i["c"] for i in items) / len(items)
    return text, conf


OCR_CACHE = None  # set by the pipeline to a directory; OCR output is cached by file fingerprint


def warm_up():
    """Load the OCR models in the background at server start so the first scan isn't slow."""
    try:
        _ocr()
    except Exception:
        pass


def read_document(data: bytes, filename: str, log=None) -> dict:
    import hashlib
    import json as _json

    log = log or (lambda *a, **k: None)
    key = hashlib.sha256(data).hexdigest()
    cache_file = OCR_CACHE / f"{key[:24]}.json" if OCR_CACHE else None
    if cache_file and cache_file.exists():
        out = _json.loads(cache_file.read_text(encoding="utf-8"))
        log(f"OCR cache hit - this exact file was OCR'd before; reusing that text", "ok")
        out["ocr_cached"] = True
        return out
    out = _read_document(data, filename, log)
    if cache_file and out["method"] != "text-layer":
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(_json.dumps(out), encoding="utf-8")
    return out


def _read_document(data: bytes, filename: str, log) -> dict:
    name = filename.lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")):
        from PIL import Image

        log("Image file - running OCR (text detection + recognition)…")
        text, conf = _ocr_image(Image.open(io.BytesIO(data)))
        return {"text": text, "method": "ocr", "pages": 1, "ocr_confidence": round(conf, 3), "page_methods": ["ocr"]}

    import pdfplumber

    texts, methods, confs = [], [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            if len(t.strip()) >= 25:
                texts.append(t)
                methods.append("text-layer")
                continue
            # No usable text layer -> scanned page. Render and OCR it.
            import pypdfium2 as pdfium

            log(f"Page {i + 1}: no text layer - rendering and running OCR (detection + recognition)…", "warn")
            doc = pdfium.PdfDocument(data)
            img = doc[i].render(scale=1.5).to_pil()
            t, c = _ocr_image(img)
            texts.append(t)
            methods.append("ocr")
            confs.append(c)
    method = "ocr" if all(m == "ocr" for m in methods) else ("mixed" if "ocr" in methods else "text-layer")
    return {
        "text": "\n".join(texts),
        "method": method,
        "pages": n,
        "ocr_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "page_methods": methods,
    }


# ----------------------------------------------------------------------------- helpers
NUM_TOKEN = re.compile(r"^[-(]?(?:[£$€]|USD|GBP|EUR|INR)?\s?\d[\d,]*(?:\.\d+)?\)?$", re.I)


def parse_num(tok: str) -> float | None:
    t = tok.strip().replace(",", "")
    t = re.sub(r"^(USD|GBP|EUR|INR|[£$€])", "", t, flags=re.I)
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = t.strip("()-")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


MONEY_RE = re.compile(r"(?:[£$€]|USD|GBP|EUR|INR)?\s?\d{1,3}(?:,\d{3})*\.\d{2}|(?:[£$€]|USD|GBP|EUR|INR)\s?\d+(?:\.\d{2})?", re.I)


def last_money(line: str) -> float | None:
    found = MONEY_RE.findall(line)
    return parse_num(found[-1].replace(" ", "")) if found else None


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})|(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})|"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})|"
    r"(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4})",
    re.I,
)


def parse_date(raw: str, day_first: bool) -> tuple[date | None, bool]:
    """Returns (date, ambiguous). Numeric dates like 04/05/2026 are ambiguous; we resolve
    with the vendor's locale (day_first) and report the ambiguity."""
    raw = raw.strip().rstrip(",")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.strptime(raw, "%Y-%m-%d").date(), False
        m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", raw)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y = y + 2000 if y < 100 else y
            if a > 12:
                return date(y, b, a), False
            if b > 12:
                return date(y, a, b), False
            ambiguous = a != b
            return (date(y, b, a) if day_first else date(y, a, b)), ambiguous
        m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", raw, re.I)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))), False
        m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})", raw, re.I)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1))), False
    except (ValueError, KeyError):
        pass
    return None, False


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ----------------------------------------------------------------------------- rules extraction
INV_NO_RE = re.compile(r"\b(?:invoice|inv)\.?\s*(?:no\.?|number|num|#)?\s*[:#]?\s*([A-Z]{0,6}[-/]?\d[A-Z0-9\-/]{2,})", re.I)
PO_RE = re.compile(r"(?<![\d-])(?:PO[-\s]?)?(4500)[\s\-]?(\d{4})(?!\d)", re.I)
TAXID_RE = re.compile(r"\b(\d{2}-\d{7})\b|\b(GB\s?\d{3}\s?\d{4}\s?\d{2})\b", re.I)
BANK_RE = re.compile(r"(?:account|acct|a/c)[^\d\n]{0,15}(\d{4})\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _field(value, line=None, conf=None):
    return {"value": value, "evidence": line, "confidence": conf}


def extract_rules(text: str, ocr: bool = False) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    base_conf = 0.8 if ocr else 0.95
    out: dict = {}

    # header lines = likely vendor name
    out["vendor_name"] = _field(next((l for l in lines if not re.search(r"invoice|statement|^bill", l, re.I)), None), None, 0.6)

    # invoice number — must contain a digit and not be a date/PO
    inv = None
    for l in lines:
        for m in INV_NO_RE.finditer(l):
            cand = m.group(1).strip(" .,:")
            if re.search(r"\d", cand) and not DATE_RE.fullmatch(cand) and not PO_RE.fullmatch(cand) and not re.match(r"date", cand, re.I):
                inv = _field(cand.upper(), l, base_conf)
                break
        if inv:
            break
    out["invoice_number"] = inv or _field(None)

    # invoice date — line labelled date (not due/service/period)
    dt = None
    for l in lines:
        if re.search(r"\bdate\b", l, re.I) and not re.search(r"due|delivery|service|period|ship", l, re.I):
            m = DATE_RE.search(l[re.search(r"\bdate\b", l, re.I).start():])
            if m:
                dt = _field(m.group(0), l, base_conf)
                break
    out["invoice_date_raw"] = dt or _field(None)

    # terms
    m = next((re.search(r"net\s*(\d{1,3})|within\s*(\d{1,3})\s*days", l, re.I) for l in lines if re.search(r"net\s*\d|within\s*\d+\s*days", l, re.I)), None)
    out["payment_terms_days"] = _field(int(m.group(1) or m.group(2)) if m else None, None, base_conf if m else None)

    # PO references (all candidates; vendors write them many ways: PO-4500-0198, 4500-0175, P.O. 4500 0217)
    pos = []
    for l in lines:
        for m in PO_RE.finditer(l):
            po = f"PO-{m.group(1)}-{m.group(2)}"
            if po not in [p["value"] for p in pos]:
                pos.append(_field(po, l, base_conf))
    out["po_references"] = pos

    # tax id, bank account, emails
    m = next((TAXID_RE.search(l) for l in lines if TAXID_RE.search(l)), None)
    out["vendor_tax_id"] = _field((m.group(1) or m.group(2)) if m else None, m.string if m else None, base_conf if m else None)
    bank = None
    for l in lines:
        mm = BANK_RE.search(l)
        if mm and re.search(r"remit|bank|ach|wire|account ending|sort|payment|iban", l, re.I):
            bank = _field("****" + mm.group(1), l, base_conf)
            break
    out["bank_account"] = bank or _field(None)
    out["emails"] = sorted({e.lower().rstrip(".") for e in EMAIL_RE.findall(text)})

    # currency
    cur = None
    for code, pat in [("GBP", r"GBP|£"), ("EUR", r"EUR|€"), ("INR", r"INR|₹"), ("USD", r"USD|\$")]:
        if re.search(pat, text):
            cur = code
            break
    out["currency"] = _field(cur, None, base_conf if cur else None)

    # tax treatment
    inclusive = bool(re.search(r"(prices?|amounts?)\s*include\s*(vat|tax|gst)|inc(l\.?|lusive)?\s*vat|incl\.?\s*(tax|gst)", text, re.I))
    out["tax_inclusive"] = inclusive
    rate = re.search(r"(?:vat|tax|gst)[^\n%]{0,20}?(\d{1,2}(?:\.\d{1,2})?)\s?%", text, re.I) or re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s?%\s*(?:vat|tax|gst)", text, re.I)
    out["tax_rate_stated"] = float(rate.group(1)) if rate else None

    # totals
    sub = tax = total = None
    for l in lines:
        low = l.lower()
        v = last_money(l)
        if v is None:
            continue
        if re.search(r"sub\s*-?total|net amount|total net|total excl", low):
            sub = sub or _field(v, l, base_conf)
        elif re.search(r"of which (vat|tax)|vat included|included vat", low):
            tax = tax or _field(v, l, base_conf)
        elif re.search(r"\b(sales\s*tax|vat|gst|tax)\b", low) and not re.search(r"tax id|vat reg|incl|total|ein", low):
            tax = tax or _field(v, l, base_conf)
        elif re.search(r"total|amount due|balance due|grand total", low):
            total = _field(v, l, base_conf)  # keep the last 'total' line (usually the grand total)
    out["subtotal"] = sub or _field(None)
    out["tax_amount"] = tax or _field(None)
    out["total"] = total or _field(None)

    # line items: rows ending in qty, unit price, amount (qty*unit ~= amount), or a single amount (bundled line)
    items = []
    in_table = False
    for l in lines:
        low = l.lower()
        if re.search(r"description|service|item", low) and re.search(r"amount|total|price|rate", low):
            in_table = True
            continue
        if not in_table:
            continue
        if re.search(r"sub\s*-?total|^total|amount due|balance due|sales tax|^tax|of which|total \(", low):
            break
        toks = l.split()
        nums = []
        while toks and NUM_TOKEN.match(toks[-1]) and len(nums) < 3:
            nums.insert(0, toks.pop())
        desc = " ".join(toks).strip()
        vals = [parse_num(n) for n in nums]
        sku = None
        if toks and re.fullmatch(r"[A-Z]{2,}[A-Z0-9]*-[A-Z0-9-]+", toks[0]):
            sku = toks[0]
            desc = " ".join(toks[1:])
        if len(vals) == 3 and all(v is not None for v in vals) and abs(vals[0] * vals[1] - vals[2]) <= max(0.02, 0.005 * abs(vals[2])):
            items.append({"sku": sku, "description": desc, "qty": vals[0], "unit_price": vals[1], "amount": vals[2], "evidence": l})
        elif len(vals) >= 1 and vals[-1] is not None and desc and "." in nums[-1]:
            items.append({"sku": sku, "description": desc, "qty": 1, "unit_price": vals[-1], "amount": vals[-1], "evidence": l, "bundled": True})
    out["line_items"] = items
    return out


# ----------------------------------------------------------------------------- LLM extraction
LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": ["string", "null"]},
        "vendor_tax_id": {"type": ["string", "null"]},
        "invoice_number": {"type": ["string", "null"]},
        "invoice_date_raw": {"type": ["string", "null"], "description": "exactly as printed"},
        "payment_terms_days": {"type": ["integer", "null"]},
        "po_references": {"type": "array", "items": {"type": "string"}},
        "currency": {"type": ["string", "null"]},
        "tax_inclusive": {"type": "boolean"},
        "tax_rate_stated": {"type": ["number", "null"]},
        "subtotal": {"type": ["number", "null"]},
        "tax_amount": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
        "bank_account_last4": {"type": ["string", "null"]},
        "emails": {"type": "array", "items": {"type": "string"}},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number"},
                },
            },
        },
    },
}

LLM_SYSTEM = (
    "You extract fields from vendor invoices for an accounts-payable team. Copy values exactly as printed; "
    "never infer or invent a value that is not on the document — use null when a field is absent. "
    "Unit prices and amounts are as printed (if prices include VAT, keep them gross and set tax_inclusive=true). "
    "PO references: return every purchase-order number you see, as printed."
)


def extract_llm(pdf_bytes: bytes) -> dict:
    from common import llm

    d = llm.complete_json(LLM_SYSTEM, "Extract the invoice fields.", LLM_SCHEMA, pdf_bytes=pdf_bytes)
    f = lambda v, conf=0.9: _field(v, "Claude (document read)", conf if v is not None else None)  # noqa: E731
    pos = []
    for p in d.get("po_references") or []:
        m = PO_RE.search(p)
        if m:
            pos.append(_field(f"PO-{m.group(1)}-{m.group(2)}", p, 0.9))
    return {
        "vendor_name": f(d.get("vendor_name")),
        "invoice_number": f((d.get("invoice_number") or "").upper() or None),
        "invoice_date_raw": f(d.get("invoice_date_raw")),
        "payment_terms_days": f(d.get("payment_terms_days")),
        "po_references": pos,
        "vendor_tax_id": f(d.get("vendor_tax_id")),
        "bank_account": f("****" + d["bank_account_last4"] if d.get("bank_account_last4") else None),
        "emails": [e.lower() for e in d.get("emails") or []],
        "currency": f(d.get("currency")),
        "tax_inclusive": bool(d.get("tax_inclusive")),
        "tax_rate_stated": d.get("tax_rate_stated"),
        "subtotal": f(d.get("subtotal")),
        "tax_amount": f(d.get("tax_amount")),
        "total": f(d.get("total")),
        "line_items": [dict(li, evidence="Claude (document read)") for li in d.get("line_items") or []],
    }
