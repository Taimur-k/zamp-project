"""Read attached vendor documents: text extraction (text layer, then OCR), type
classification, and labelled-field extraction.

The extraction is deliberately label-driven and tolerant (synonym lists, case
insensitive, value on same line) so it works on the kind of documents vendors
actually send - not just on one template.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def _ocr_image(img) -> str:
    """OCR a PIL image; rebuild lines by grouping boxes with similar y."""
    import numpy as np

    res, _ = _ocr_engine()(np.array(img.convert("RGB")))
    if not res:
        return ""
    items = []
    for box, text, score in res:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append(((min(ys) + max(ys)) / 2, min(xs), max(ys) - min(ys), text))
    items.sort()
    lines, cur, cur_y = [], [], None
    for y, x, h, t in items:
        if cur_y is not None and abs(y - cur_y) > max(8, h * 0.6):
            lines.append(" ".join(t for _, t in sorted(cur)))
            cur = []
        cur.append((x, t))
        cur_y = y if cur_y is None else (cur_y + y) / 2
    if cur:
        lines.append(" ".join(t for _, t in sorted(cur)))
    return "\n".join(lines)


CACHE_DIR = Path(__file__).resolve().parent / ".ocr_cache"


def _crop(img):
    """Trim the white page margin around a scanned image (faster, cleaner OCR)."""
    from PIL import ImageOps

    bbox = ImageOps.invert(img.convert("L")).point(lambda p: 255 if p > 12 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def extract_text(path: Path) -> tuple[str, str]:
    """Return (text, method). Text layer first; OCR if the PDF is a scan or the file is an image.

    OCR output is cached by file hash: a document is only OCR'd once, however many times it is re-checked."""
    import hashlib

    key = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    cached = CACHE_DIR / f"{key}.txt"
    if cached.exists():
        method, _, text = cached.read_text(encoding="utf-8").partition("\n")
        return text, f"{method} (cached result)"
    text, method = _extract_uncached(path)
    if method.startswith("OCR"):
        CACHE_DIR.mkdir(exist_ok=True)
        cached.write_text(method + "\n" + text, encoding="utf-8")
    return text, method


def _extract_uncached(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
        from PIL import Image

        return _ocr_image(Image.open(path)), "OCR (image)"
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if len(text.strip()) >= 40:
        return text, "PDF text layer"
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    parts = []
    for i in range(min(len(doc), 3)):
        parts.append(_ocr_image(_crop(doc[i].render(scale=1.5).to_pil())))
    return "\n".join(parts), "OCR (scanned PDF, no text layer)"


# ---------------------------------------------------------------- classification
DOC_TYPES = {
    "tax_document": "Tax registration (W-9 / VAT / GST certificate)",
    "bank_proof": "Bank proof (bank letter / cancelled cheque)",
    "incorporation": "Certificate of incorporation",
    "insurance": "Certificate of insurance",
}

_SIGNALS = {
    "tax_document": [r"\bW-9\b", r"taxpayer identification", r"VAT registration", r"\bGSTIN\b", r"REG-06",
                     r"Umsatzsteuer", r"USt-IdNr", r"value added tax", r"goods and services tax"],
    "bank_proof": [r"\bcheque\b", r"\bVOID\b", r"account\s*confirmation", r"bank\s*confirmation", r"account\s*verification",
                   r"\bIFSC", r"sort\s*code", r"routing", r"or\s*bearer", r"account\s*(holder|name|title)", r"\bIBAN\b",
                   r"A/c\s*No", r"C?ANCELL?ED", r"Kontoinhaber", r"Bankbest", r"confirmation\s*of\s*account"],
    "incorporation": [r"certificate of incorporation", r"certificate of formation", r"articles of organi[sz]ation",
                      r"Companies House", r"Handelsregister", r"\bCIN\b", r"Registrar of Companies", r"Registergericht",
                      r"Secretary of State", r"Division of Corporations", r"duly (formed|incorporated)", r"Corporate Identity Number",
                      r"Date of (Incorporation|Formation)", r"Company Number", r"Eintragungsdatum", r"\bHRB\b"],
    "insurance": [r"certificate of (liability )?insurance", r"\binsurer\b", r"policy (number|period)", r"named insured",
                  r"coverage", r"limits? of liability"],
}


def classify(text: str) -> tuple[str | None, dict]:
    scores = {k: sum(1 for p in pats if re.search(p, text, re.I)) for k, pats in _SIGNALS.items()}
    best = max(scores, key=scores.get)
    runner_up = max(v for k, v in scores.items() if k != best)
    confident = scores[best] >= 2 or (scores[best] == 1 and runner_up == 0)
    return (best if confident and scores[best] > runner_up else None), scores


# ---------------------------------------------------------------- field extraction
LABELS = {
    "name": ["Legal Name of Business", "Legal name", "Name of Business", "Registered Name", "Company Name",
             "Name of Company", "Named Insured", "Insured", "Business name", "Firmenname", "Name"],
    "holder": ["Account Holder Name", "Account Holder", "Account Name", "Account Title", "Beneficiary Name",
               "Kontoinhaber", "Payee"],
    "tax_id": ["Employer identification number", "EIN", "GSTIN", "VAT Registration Number", "VAT Number", "VAT No",
               "USt-IdNr", "Tax ID"],
    "reg_no": ["Company Number", "Corporate Identity Number", "CIN", "File Number", "Registration Number",
               "HRB", "Registernummer"],
    "account": ["Account Number", "A/c No", "Account No", "Kontonummer"],
    "iban": ["IBAN"],
    "routing": ["Routing Number", "ABA Routing", "ABA"],
    "ifsc": ["IFSC Code", "IFSC"],
    "sort_code": ["Sort Code"],
    "expiry": ["Expiry Date", "Expiration Date", "Policy Expiry", "Valid Until", "Policy Period To", "Expires"],
    "inc_date": ["Date of Incorporation", "Incorporated on", "Date of Formation", "Eintragungsdatum", "Registration Date"],
}


def _label_value(text: str, labels: list[str]) -> str | None:
    for lab in labels:
        pat = re.escape(lab).replace("\\ ", r"\s*")
        m = re.search(rf"(?im)^[^\n]*?(?<![A-Za-z]){pat}(?![a-z])[^\S\n]*(?:\([^)\n]*\))?[^\S\n]*[:\-][^\S\n]*(.+)$", text)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def extract_fields(text: str) -> dict:
    return {k: _label_value(text, v) for k, v in LABELS.items()}


_MONTHS = "january february march april may june july august september october november december".split()


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:30].strip().rstrip("."), fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return datetime.strptime(m.group(0), "%Y-%m-%d").date()
    return None


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def alnum(s: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()
