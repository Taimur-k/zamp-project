"""Deterministic validators: tax IDs, bank identifiers, names, emails.

Every function here is pure and explainable - each returns (ok, reason) or a
small dict, so the pipeline can show *why* something passed or failed.
"""
from __future__ import annotations

import re
import string
from difflib import SequenceMatcher

# ---------------------------------------------------------------- names
LEGAL_SUFFIXES = {
    "ltd", "limited", "llc", "l.l.c", "inc", "incorporated", "corp", "corporation", "co", "company",
    "pvt", "private", "plc", "gmbh", "ag", "llp", "lp", "sa", "sas", "sarl", "bv", "nv", "srl", "spa",
    "pte", "pty", "kg", "ug", "the", "m/s", "ms",
}


def normalize_name(name: str) -> str:
    """Lower-case, '&'->'and', drop punctuation and legal-form suffixes.

    'SHAKTI PRECISION COMPONENTS PRIVATE LIMITED' and 'Shakti Precision
    Components Pvt. Ltd.' both become 'shakti precision components'.
    """
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s/]", " ", s)
    toks = [t for t in s.split() if t not in LEGAL_SUFFIXES]
    return " ".join(toks)


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity of two company names after normalisation.

    Takes the best of a character-level ratio (catches typos) and a
    token-sorted ratio (catches word re-ordering)."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    r1 = SequenceMatcher(None, na, nb).ratio()
    r2 = SequenceMatcher(None, " ".join(sorted(na.split())), " ".join(sorted(nb.split()))).ratio()
    return round(max(r1, r2), 3)


def best_line_match(name: str, text: str) -> tuple[float, str]:
    """Find the line (or pair of adjacent lines) in `text` most similar to `name`."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    cands = lines + [f"{a} {b}" for a, b in zip(lines, lines[1:])]
    best, best_ln = 0.0, ""
    target = normalize_name(name)
    for ln in cands:
        n = normalize_name(ln)
        if not n:
            continue
        # also try the substring of the line that has the same token count as the name
        toks = n.split()
        k = len(target.split())
        windows = [" ".join(toks[i:i + k]) for i in range(max(1, len(toks) - k + 1))] if k else [n]
        for w in windows + [n]:
            r = SequenceMatcher(None, target, w).ratio()
            if r > best:
                best, best_ln = r, ln
    return round(best, 3), best_ln


# ---------------------------------------------------------------- tax IDs
# IRS campus prefixes that have been assigned for EINs.
EIN_VALID_PREFIXES = {
    *[f"{i:02d}" for i in range(1, 7)], *[f"{i:02d}" for i in range(10, 17)], *[f"{i:02d}" for i in range(20, 28)],
    *[f"{i:02d}" for i in range(30, 49)], *[f"{i:02d}" for i in range(50, 69)], *[f"{i:02d}" for i in range(71, 78)],
    *[f"{i:02d}" for i in range(80, 89)], *[f"{i:02d}" for i in range(90, 96)], "98", "99",
}

GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry", "36": "Telangana", "37": "Andhra Pradesh",
}
# Two-letter state codes used inside an Indian CIN (company identification number)
CIN_STATES = {"MH": "Maharashtra", "KA": "Karnataka", "DL": "Delhi", "TN": "Tamil Nadu", "GJ": "Gujarat",
              "TG": "Telangana", "HR": "Haryana", "UP": "Uttar Pradesh", "WB": "West Bengal", "KL": "Kerala",
              "RJ": "Rajasthan", "AP": "Andhra Pradesh", "GA": "Goa", "PB": "Punjab"}
PAN_ENTITY = {"C": "Company", "P": "Individual", "F": "Firm/LLP", "H": "HUF", "A": "Association of persons",
              "T": "Trust", "B": "Body of individuals", "L": "Local authority", "J": "Artificial juridical person", "G": "Government"}

EU_VAT_FORMATS = {
    "DE": r"^DE\d{9}$", "FR": r"^FR[0-9A-Z]{2}\d{9}$", "NL": r"^NL\d{9}B\d{2}$", "IT": r"^IT\d{11}$",
    "ES": r"^ES[0-9A-Z]\d{7}[0-9A-Z]$", "IE": r"^IE\d{7}[A-W][A-I]?$", "BE": r"^BE[01]\d{9}$", "AT": r"^ATU\d{8}$",
}

GST_CHARSET = string.digits + string.ascii_uppercase


def clean_id(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, c in enumerate(first14):
        v = GST_CHARSET.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return GST_CHARSET[(36 - total % 36) % 36]


def de_vat_check_digit(first8: str) -> int:
    """ISO 7064 MOD 11,10 as used by the German USt-IdNr."""
    product = 10
    for d in first8:
        s = (int(d) + product) % 10
        s = 10 if s == 0 else s
        product = (2 * s) % 11
    check = 11 - product
    return 0 if check == 10 else check


def uk_vat_valid(nine: str) -> bool:
    total = sum(int(d) * w for d, w in zip(nine[:7], range(8, 1, -1))) + int(nine[7:9])
    return total % 97 == 0 or (total + 55) % 97 == 0


def validate_tax_id(country: str, tax_id: str, vendor_type: str = "company") -> list[dict]:
    """Return a list of check dicts {name, ok, detail, kind} for this tax ID."""
    out: list[dict] = []
    tid = clean_id(tax_id)
    c = (country or "").upper()

    def add(name, ok, detail, kind="format", extra=None, bad=""):
        d = {"name": name, "ok": ok, "detail": detail, "kind": kind, "bad": bad or f"fails: {name.lower()}"}
        if extra:
            d.update(extra)
        out.append(d)

    if not tid:
        add("Tax ID present", False, "No tax ID supplied", "missing")
        return out

    if c == "US":
        ok = bool(re.fullmatch(r"\d{9}", tid))
        add("EIN format (9 digits, XX-XXXXXXX)", ok, f"{tax_id!r} -> {tid}", bad="should be 9 digits in the form XX-XXXXXXX")
        if ok:
            p = tid[:2]
            add("EIN prefix is an IRS-assigned campus code", p in EIN_VALID_PREFIXES,
                f"prefix {p} {'is' if p in EIN_VALID_PREFIXES else 'is NOT'} assigned by the IRS", "checksum",
                bad=f"starts with {p}, a prefix the IRS never issues")
    elif c == "GB":
        body = tid[2:] if tid.startswith("GB") else tid
        ok = bool(re.fullmatch(r"\d{9}", body))
        add("UK VAT format (GB + 9 digits)", ok, f"{tax_id!r}", bad="should be GB followed by 9 digits")
        if ok:
            v = uk_vat_valid(body)
            add("UK VAT mod-97 check digits", v, f"weighted sum check on {body} {'passes' if v else 'fails'}", "checksum",
                bad="fails the VAT check-digit test, so at least one digit is mistyped")
    elif c == "IN":
        ok = bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", tid))
        add("GSTIN format (15 chars: state+PAN+entity+Z+check)", ok, tid,
            bad="should be 15 characters: 2-digit state code, 10-character PAN, entity number, 'Z' and a check character")
        if ok:
            exp = gstin_check_char(tid[:14])
            add("GSTIN check character", exp == tid[14], f"computed '{exp}', supplied '{tid[14]}'", "checksum",
                {"expected_check": exp}, bad="fails the GSTIN check-character test, so a character is mistyped")
            st = tid[:2]
            add("GSTIN state code is a real state", st in GST_STATES, f"{st} = {GST_STATES.get(st, 'unknown')}", "format",
                {"state_code": st, "state_name": GST_STATES.get(st)}, bad=f"starts with {st}, which is not a valid state code")
            pan = tid[2:12]
            ent = pan[3]
            want = "P" if vendor_type == "sole_proprietor" else None
            okent = (ent == want) if want else ent in ("C", "F")
            add("Embedded PAN entity type matches vendor type", okent,
                f"PAN {pan}: 4th char '{ent}' = {PAN_ENTITY.get(ent, 'unknown')}; vendor type '{vendor_type}'", "consistency",
                {"pan": pan}, bad=f"contains the PAN of a {PAN_ENTITY.get(ent, 'different entity type').lower()}, but you registered as a {vendor_type.replace('_', ' ')}")
    elif c in EU_VAT_FORMATS:
        pat = EU_VAT_FORMATS[c]
        t = tid if tid.startswith(c) or (c == "AT") else c + tid
        ok = bool(re.fullmatch(pat, t))
        add(f"EU VAT format ({c})", ok, t, bad=f"isn't in the {c} VAT number format")
        if ok and c == "DE":
            exp = de_vat_check_digit(t[2:10])
            add("German USt-IdNr check digit (ISO 7064 MOD 11,10)", exp == int(t[10]),
                f"computed {exp}, supplied {t[10]}", "checksum", bad="fails the German VAT check digit, so a digit is mistyped")
    else:
        add("Tax ID country supported", False, f"No validation rules configured for country '{c}'", "unsupported")
    return out


def gstin_state_vs_address(tax_id: str, state: str) -> tuple[bool, str]:
    tid = clean_id(tax_id)
    code = tid[:2]
    name = GST_STATES.get(code)
    if not name or not state:
        return True, "not checkable"
    ok = normalize_name(name) == normalize_name(state)
    return ok, f"GSTIN state {code} = {name}; address state = {state}"


# ---------------------------------------------------------------- bank identifiers
IBAN_LENGTHS = {"GB": 22, "DE": 22, "FR": 27, "NL": 18, "IT": 27, "ES": 24, "IE": 22, "BE": 16, "AT": 20, "CH": 21}
IFSC_TO_SWIFT = {"HDFC": "HDFC", "ICIC": "ICIC", "SBIN": "SBIN", "UTIB": "AXIS", "KKBK": "KKBK", "YESB": "YESB", "PUNB": "PUNB"}
IFSC_BANK_NAMES = {"HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "SBIN": "State Bank of India", "UTIB": "Axis Bank",
                   "KKBK": "Kotak Mahindra Bank", "YESB": "Yes Bank", "PUNB": "Punjab National Bank"}


def iban_valid(iban: str) -> tuple[bool, str]:
    s = clean_id(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False, "not an IBAN shape"
    cc = s[:2]
    if cc in IBAN_LENGTHS and len(s) != IBAN_LENGTHS[cc]:
        return False, f"{cc} IBANs are {IBAN_LENGTHS[cc]} characters, got {len(s)}"
    rearr = s[4:] + s[:4]
    num = "".join(str(int(ch, 36)) for ch in rearr)
    ok = int(num) % 97 == 1
    return ok, "mod-97 = 1" if ok else f"mod-97 = {int(num) % 97} (should be 1) - a digit is wrong"


def iban_check_digits(country: str, bban: str) -> str:
    num = "".join(str(int(ch, 36)) for ch in (bban + country + "00"))
    return f"{98 - int(num) % 97:02d}"


def aba_valid(routing: str) -> tuple[bool, str]:
    r = re.sub(r"\D", "", routing or "")
    if len(r) != 9:
        return False, f"routing numbers are 9 digits, got {len(r)}"
    d = [int(x) for x in r]
    s = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    if s % 10:
        return False, f"ABA checksum 3-7-1 weighted sum = {s}, not a multiple of 10"
    p = int(r[:2])
    if not (0 <= p <= 12 or 21 <= p <= 32 or 61 <= p <= 72 or p == 80):
        return False, f"prefix {r[:2]} is not a Federal Reserve district code"
    return True, f"3-7-1 checksum = {s} (multiple of 10)"


def ifsc_valid(ifsc: str) -> tuple[bool, str]:
    s = clean_id(ifsc)
    ok = bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", s))
    return ok, f"{s}: 4-letter bank code + '0' + 6-char branch" if ok else f"{s!r} is not 4 letters + 0 + 6 characters"


def swift_parse(swift: str) -> dict | None:
    s = clean_id(swift)
    m = re.fullmatch(r"([A-Z]{4})([A-Z]{2})([A-Z0-9]{2})([A-Z0-9]{3})?", s)
    if not m:
        return None
    return {"bank": m.group(1), "country": m.group(2), "location": m.group(3), "branch": m.group(4) or "XXX", "swift": s}


def sort_code_clean(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def bank_key(sub: dict) -> str | None:
    """Canonical key for "this exact account", comparable across formats.

    A UK IBAN and the same account given as sort code + account number map to
    the same key, so a fraudster can't dodge the duplicate check by switching format.
    """
    iban = clean_id(sub.get("iban"))
    if iban.startswith("GB") and len(iban) == 22:
        return f"GB:{iban[8:14]}:{iban[14:]}"
    if iban:
        return f"IBAN:{iban}"
    acct = re.sub(r"\D", "", sub.get("account_number") or "")
    if not acct:
        return None
    if sub.get("routing_number"):
        return f"US:{re.sub(r'[^0-9]', '', sub['routing_number'])}:{acct}"
    if sub.get("ifsc"):
        return f"IN:{clean_id(sub['ifsc'])}:{acct}"
    if sub.get("sort_code"):
        return f"GB:{sort_code_clean(sub['sort_code'])}:{acct}"
    return f"ACCT:{acct}"


# ---------------------------------------------------------------- contact
FREE_EMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "proton.me", "protonmail.com",
              "icloud.com", "gmx.de", "web.de", "rediffmail.com", "yahoo.co.in", "yahoo.co.uk", "mail.com", "zoho.com"}


def email_valid(e: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", e or ""))


def domain_of_url(url: str) -> str:
    d = re.sub(r"^https?://", "", (url or "").strip().lower()).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def registrable(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
