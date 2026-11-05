"""Generate the prepared vendor submissions and their supporting documents.

Run:  python ps2_vendor/samples/make_samples.py
Creates samples/<scenario>.json (the form submission) and samples/docs/<vendor>/*.pdf
(W-9s, VAT/GST certificates, bank letters, a *scanned* cancelled cheque with no
text layer, insurance certificates, incorporation certificates). All identifiers
are synthetic; checksums are computed so "valid" numbers really are valid.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"


# ---------------------------------------------------------------- pdf helpers
def _doc(path: Path, *, issuer: str, issuer_sub: str, title: str, rows: list[tuple[str, str]], paras: list[str] = (),
         accent=colors.HexColor("#1f3a68"), size=A4, sign: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=size)
    w, h = size
    c.setFillColor(accent)
    c.rect(0, h - 70, w, 70, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, h - 38, issuer)
    c.setFont("Helvetica", 9)
    c.drawString(40, h - 54, issuer_sub)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, h - 110, title)
    y = h - 145
    c.setFont("Helvetica", 10.5)
    for p in paras[:1]:
        for line in _wrap(p, 95):
            c.drawString(40, y, line)
            y -= 15
        y -= 8
    for k, v in rows:
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(40, y, f"{k}:")
        vx = max(230, 40 + c.stringWidth(f"{k}:", "Helvetica-Bold", 10.5) + 12)
        c.setFont("Helvetica", 10.5)
        c.drawString(vx, y, v)
        y -= 19
    y -= 10
    c.setFont("Helvetica", 10.5)
    for p in paras[1:]:
        for line in _wrap(p, 95):
            c.drawString(40, y, line)
            y -= 15
        y -= 8
    if sign:
        y -= 20
        c.line(40, y, 220, y)
        c.setFont("Helvetica", 9)
        c.drawString(40, y - 12, sign)
    c.setFont("Helvetica-Oblique", 7.5)
    c.setFillColor(colors.grey)
    c.drawString(40, 30, "SAMPLE DOCUMENT - synthetic data generated for an onboarding-automation case study. Not a real record.")
    c.save()


def _wrap(text: str, n: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 > n:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    return lines


def w9(path, name, business, classification, address, city_line, ein, signer, date):
    _doc(path, issuer="Form W-9 (Rev. March 2024)", issuer_sub="Department of the Treasury - Internal Revenue Service",
         title="Request for Taxpayer Identification Number and Certification", accent=colors.HexColor("#333333"), size=letter,
         rows=[("Name (as shown on your income tax return)", name), ("Business name/disregarded entity name", business or "-"),
               ("Federal tax classification", classification), ("Address", address), ("City, state, and ZIP code", city_line),
               ("Part I - Employer identification number", ein), ("Signature of U.S. person", signer), ("Date", date)],
         paras=["Give form to the requester. Do not send to the IRS.",
                "Under penalties of perjury, I certify that the number shown on this form is my correct taxpayer identification "
                "number and I am not subject to backup withholding."])


def _font(size, bold=False):
    for f in (("arialbd.ttf" if bold else "arial.ttf"), ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def scanned_cheque(path: Path, bank: str, branch: str, ifsc: str, account: str, holder: str, micr: str):
    """A cancelled cheque as a *photo/scan*: an image-only PDF, so text must be OCR'd."""
    W, H = 1600, 720
    img = Image.new("RGB", (W, H), (236, 242, 248))
    d = ImageDraw.Draw(img)
    for x in range(0, W, 24):  # security pattern
        d.line([(x, 0), (x - 300, H)], fill=(226, 234, 244), width=2)
    d.rectangle([10, 10, W - 10, H - 10], outline=(60, 80, 120), width=4)
    d.text((40, 30), bank, font=_font(44, True), fill=(20, 40, 110))
    d.text((40, 90), branch, font=_font(26), fill=(30, 30, 30))
    d.text((40, 130), f"IFSC: {ifsc}", font=_font(30, True), fill=(20, 20, 20))
    d.text((1180, 40), "Date  __ __ ____", font=_font(28), fill=(30, 30, 30))
    d.text((40, 210), "Pay ____________________________________________  or Bearer", font=_font(28), fill=(30, 30, 30))
    d.text((40, 270), "Rupees __________________________________________", font=_font(28), fill=(30, 30, 30))
    d.rectangle([1180, 260, 1560, 320], outline=(30, 30, 30), width=2)
    d.text((40, 360), f"A/c No: {account}", font=_font(36, True), fill=(10, 10, 10))
    d.text((40, 420), "Current Account", font=_font(26), fill=(30, 30, 30))
    d.text((980, 470), f"For {holder}", font=_font(26, True), fill=(10, 10, 10))
    d.text((1120, 560), "Authorised Signatory", font=_font(24), fill=(30, 30, 30))
    d.text((300, 640), micr, font=_font(34), fill=(20, 20, 20))
    # big diagonal CANCELLED stamp
    stamp = Image.new("RGBA", (900, 160), (0, 0, 0, 0))
    ImageDraw.Draw(stamp).text((10, 10), "CANCELLED", font=_font(130, True), fill=(200, 30, 30, 110))
    img.paste(stamp.rotate(12, expand=True), (330, 150), stamp.rotate(12, expand=True))
    img = img.rotate(-0.6, fillcolor=(250, 250, 250))  # slightly skewed, like a phone scan
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawImage(ImageReader(buf), 30, 400, width=535, height=535 * H / W)
    c.save()


# ---------------------------------------------------------------- vendors
def northwind():
    d = DOCS / "northwind"
    w9(d / "w9.pdf", "Northwind Analytics LLC", "", "Limited liability company - C corporation",
       "1450 Market Street, Suite 800", "San Francisco, CA 94103", "47-3829105", "Priya Raman", "10/21/2026")
    _doc(d / "bank_letter.pdf", issuer="Wells Fargo Bank, N.A.", issuer_sub="Commercial Banking - 420 Montgomery Street, San Francisco, CA 94104",
         title="Bank Account Confirmation Letter", accent=colors.HexColor("#b31b1b"),
         paras=["To whom it may concern,", "This letter confirms that the account below is held with Wells Fargo Bank, N.A. "
                "and is in good standing as of the date of this letter."],
         rows=[("Account Holder Name", "Northwind Analytics LLC"), ("Account Number", "4417092235"),
               ("ABA Routing Number", "121000248"), ("SWIFT/BIC", "WFBIUS6S"), ("Account Type", "Business Checking"),
               ("Date", "October 19, 2026")], sign="Relationship Manager, Commercial Banking")
    _doc(d / "certificate_of_insurance.pdf", issuer="Pacific Mutual Insurance Co.", issuer_sub="Certificate of Liability Insurance - ACORD-style summary",
         title="Certificate of Liability Insurance", accent=colors.HexColor("#0b5d4b"),
         paras=["This certificate is issued as a matter of information only and confers no rights upon the certificate holder."],
         rows=[("Named Insured", "Northwind Analytics LLC"), ("Insurer", "Pacific Mutual Insurance Co."),
               ("Policy Number", "GL-88213-4471"), ("Coverage", "Commercial General Liability"),
               ("Limits of Liability", "USD 2,000,000 each occurrence"), ("Effective Date", "2026-02-01"),
               ("Expiry Date", "2028-01-31")], sign="Authorized Representative")
    _doc(d / "certificate_of_formation.pdf", issuer="State of Delaware", issuer_sub="Secretary of State - Division of Corporations",
         title="Certificate of Formation of a Limited Liability Company", accent=colors.HexColor("#22314f"),
         paras=["I, Secretary of State of the State of Delaware, do hereby certify the following entity was duly formed."],
         rows=[("Company Name", "Northwind Analytics LLC"), ("File Number", "7731905"), ("Date of Formation", "2019-04-11"),
               ("Entity Type", "Limited Liability Company")], sign="Secretary of State")
    return {
        "submission_id": "SUB-2026-1041", "submitted_at": "2026-10-21", "resubmission_of": None,
        "vendor_type": "company", "category": "professional_services",
        "company": {"legal_name": "Northwind Analytics LLC", "trading_name": "Northwind Analytics", "registration_number": "7731905",
                    "country": "US", "address": {"line1": "1450 Market Street, Suite 800", "city": "San Francisco", "state": "CA", "postal_code": "94103"},
                    "website": "https://www.northwindanalytics.io"},
        "tax": {"tax_id": "47-3829105"},
        "contact": {"name": "Priya Raman", "title": "Head of Finance", "email": "priya.raman@northwindanalytics.io", "phone": "+1 415 555 0142"},
        "bank": {"account_holder": "Northwind Analytics LLC", "bank_name": "Wells Fargo Bank, N.A.", "bank_country": "US", "currency": "USD",
                 "account_number": "4417092235", "routing_number": "121000248", "swift": "WFBIUS6S"},
        "documents": [{"type": "tax_document", "file": "docs/northwind/w9.pdf"}, {"type": "bank_proof", "file": "docs/northwind/bank_letter.pdf"},
                      {"type": "insurance", "file": "docs/northwind/certificate_of_insurance.pdf"},
                      {"type": "incorporation", "file": "docs/northwind/certificate_of_formation.pdf"}],
    }


def shakti():
    d = DOCS / "shakti"
    _doc(d / "gst_certificate.pdf", issuer="Government of India", issuer_sub="Goods and Services Tax Network - Form GST REG-06",
         title="Registration Certificate (Form GST REG-06)", accent=colors.HexColor("#5b3a0e"),
         paras=["This is to certify that the person below has been registered under the Goods and Services Tax Act, 2017."],
         rows=[("GSTIN", "27AAKCS4821M1ZW"), ("Legal Name", "SHAKTI PRECISION COMPONENTS PRIVATE LIMITED"),
               ("Trade Name", "SHAKTI PRECISION"), ("Constitution of Business", "Private Limited Company"),
               ("Address of Principal Place", "Plot 42, MIDC Bhosari, Pune, Maharashtra, 411026"),
               ("Date of Liability", "01/07/2017"), ("Type of Registration", "Regular")], sign="Goods and Services Tax Network")
    scanned_cheque(d / "cancelled_cheque_scan.pdf", "HDFC BANK", "Bhosari MIDC Branch, Pune 411026", "HDFC0001234",
                   "50200012345678", "SHAKTI PRECISION COMPONENTS PVT LTD", "\"004517\" 411240012: 004517\" 29")
    _doc(d / "certificate_of_incorporation.pdf", issuer="Ministry of Corporate Affairs", issuer_sub="Registrar of Companies, Pune - Government of India",
         title="Certificate of Incorporation", accent=colors.HexColor("#173f5f"),
         paras=["I hereby certify that the company named below is incorporated under the Companies Act, 2013 and that the company is limited by shares."],
         rows=[("Name of Company", "SHAKTI PRECISION COMPONENTS PRIVATE LIMITED"), ("Corporate Identity Number", "U29100MH2015PTC264813"),
               ("Date of Incorporation", "14/08/2015"), ("Registered Office", "Pune, Maharashtra")], sign="Registrar of Companies")
    return {
        "submission_id": "SUB-2026-1042", "submitted_at": "2026-10-24", "resubmission_of": None,
        "vendor_type": "company", "category": "goods",
        "company": {"legal_name": "Shakti Precision Components Pvt. Ltd.", "trading_name": "Shakti Precision", "registration_number": "U29100MH2015PTC264813",
                    "country": "IN", "address": {"line1": "Plot 42, MIDC Bhosari", "city": "Pune", "state": "Maharashtra", "postal_code": "411026"},
                    "website": "https://shaktiprecision.in"},
        "tax": {"tax_id": "27AAKCS4821M1ZW"},
        "contact": {"name": "Rohan Kulkarni", "title": "Accounts Manager", "email": "rohan.k@shaktiprecision.in", "phone": "+91 20 5555 0199"},
        "bank": {"account_holder": "SHAKTI PRECISION COMPONENTS PRIVATE LIMITED", "bank_name": "HDFC Bank", "bank_country": "IN", "currency": "INR",
                 "account_number": "50200012345678", "ifsc": "HDFC0001234", "swift": "HDFCINBB"},
        "documents": [{"type": "tax_document", "file": "docs/shakti/gst_certificate.pdf"},
                      {"type": "bank_proof", "file": "docs/shakti/cancelled_cheque_scan.pdf"},
                      {"type": "incorporation", "file": "docs/shakti/certificate_of_incorporation.pdf"}],
    }


def kestrel():
    d = DOCS / "kestrel"
    _doc(d / "vat_certificate.pdf", issuer="HM Revenue & Customs", issuer_sub="VAT Registration Service",
         title="Certificate of Registration for Value Added Tax", accent=colors.HexColor("#005ea5"),
         rows=[("VAT Registration Number", "284 7193 26"), ("Legal Name", "Kestrel Facilities Management Ltd"),
               ("Trading Name", "Kestrel FM"), ("Principal place of business", "18 Wharf Road, Manchester, M1 2AB"),
               ("Effective date of registration", "2016-05-01")], sign="HM Revenue & Customs")
    _doc(d / "bank_letter.pdf", issuer="NatWest", issuer_sub="Business Banking - 1 Spinningfields Square, Manchester M3 3AP",
         title="Confirmation of Account Details", accent=colors.HexColor("#42145f"),
         paras=["Dear Sir/Madam,", "At the request of our customer we confirm the following account details. This letter is "
                "provided without liability on the part of the bank."],
         rows=[("Account Name", "Kestrel Group Holdings Ltd"), ("Sort Code", "56-00-36"), ("Account Number", "47218390"),
               ("IBAN", "GB93 NWBK 5600 3647 2183 90"), ("BIC", "NWBKGB2L"), ("Date", "12 October 2026")],
         sign="Business Banking Team")
    _doc(d / "insurance_certificate.pdf", issuer="Albion Commercial Insurance plc", issuer_sub="Employers' & Public Liability",
         title="Certificate of Insurance", accent=colors.HexColor("#7a1f3d"),
         rows=[("Named Insured", "Kestrel Facilities Management Ltd"), ("Policy Number", "ACI-PL-559201"),
               ("Coverage", "Public Liability GBP 5,000,000; Employers' Liability GBP 10,000,000"),
               ("Effective Date", "2025-04-01"), ("Expiry Date", "31 March 2026")], sign="Albion Commercial Insurance plc")
    _doc(d / "certificate_of_incorporation.pdf", issuer="Companies House", issuer_sub="Registrar of Companies for England and Wales",
         title="Certificate of Incorporation of a Private Limited Company", accent=colors.HexColor("#0b0c0c"),
         rows=[("Company Name", "KESTREL FACILITIES MANAGEMENT LTD"), ("Company Number", "09876543"),
               ("Date of Incorporation", "2015-11-23"), ("Registered office", "England and Wales")], sign="Registrar of Companies")
    # resubmission documents
    _doc(d / "bank_letter_v2.pdf", issuer="NatWest", issuer_sub="Business Banking - 1 Spinningfields Square, Manchester M3 3AP",
         title="Confirmation of Account Details", accent=colors.HexColor("#42145f"),
         paras=["Dear Sir/Madam,", "At the request of our customer we confirm the following account details."],
         rows=[("Account Name", "Kestrel Facilities Management Ltd"), ("Sort Code", "56-00-36"), ("Account Number", "71045522"),
               ("IBAN", "GB47 NWBK 5600 3671 0455 22"), ("BIC", "NWBKGB2L"), ("Date", "3 November 2026")], sign="Business Banking Team")
    _doc(d / "insurance_certificate_renewed.pdf", issuer="Albion Commercial Insurance plc", issuer_sub="Employers' & Public Liability",
         title="Certificate of Insurance", accent=colors.HexColor("#7a1f3d"),
         rows=[("Named Insured", "Kestrel Facilities Management Ltd"), ("Policy Number", "ACI-PL-602118"),
               ("Coverage", "Public Liability GBP 5,000,000; Employers' Liability GBP 10,000,000"),
               ("Effective Date", "2026-11-01"), ("Expiry Date", "31 October 2028")], sign="Albion Commercial Insurance plc")
    base = {
        "submission_id": "SUB-2026-1043", "submitted_at": "2026-10-28", "resubmission_of": None,
        "vendor_type": "company", "category": "onsite_services",
        "company": {"legal_name": "Kestrel Facilities Management Ltd", "trading_name": "Kestrel FM", "registration_number": "09876543",
                    "country": "GB", "address": {"line1": "18 Wharf Road", "city": "Manchester", "state": "", "postal_code": "M1 2AB"},
                    "website": "https://www.kestrelfm.co.uk"},
        "tax": {"tax_id": "GB284719326"},
        "contact": {"name": "Daniel Hughes", "title": "Finance Director", "email": "d.hughes@kestrelfm.co.uk", "phone": "+44 161 555 0175"},
        "bank": {"account_holder": "Kestrel Facilities Management Ltd", "bank_name": "NatWest", "bank_country": "GB", "currency": "GBP",
                 "sort_code": "56-00-36", "account_number": "47218390", "iban": "GB93NWBK56003647218390", "swift": "NWBKGB2L"},
        "documents": [{"type": "tax_document", "file": "docs/kestrel/vat_certificate.pdf"}, {"type": "bank_proof", "file": "docs/kestrel/bank_letter.pdf"},
                      {"type": "insurance", "file": "docs/kestrel/insurance_certificate.pdf"},
                      {"type": "incorporation", "file": "docs/kestrel/certificate_of_incorporation.pdf"}],
    }
    resub = json.loads(json.dumps(base))
    resub.update({"submission_id": "SUB-2026-1043-R1", "submitted_at": "2026-11-04", "resubmission_of": "SUB-2026-1043"})
    resub["bank"].update({"account_number": "71045522", "iban": "GB47NWBK56003671045522"})
    resub["documents"][1]["file"] = "docs/kestrel/bank_letter_v2.pdf"
    resub["documents"][2]["file"] = "docs/kestrel/insurance_certificate_renewed.pdf"
    return base, resub


def brightline():
    d = DOCS / "brightline"
    w9(d / "w9.pdf", "Brightline Logistics Inc", "", "C corporation", "2200 Commerce Blvd", "Newark, NJ 07114",
       "84-2290471", "Mark Delaney", "10/30/2026")
    _doc(d / "bank_letter.pdf", issuer="JPMorgan Chase Bank, N.A.", issuer_sub="Business Banking - 270 Park Avenue, New York, NY 10017",
         title="Account Verification Letter", accent=colors.HexColor("#117aca"),
         paras=["To whom it may concern,", "Please accept this letter as verification of the account below."],
         rows=[("Account Holder Name", "Brightline Logistics Inc"), ("Account Number", "483920117"),
               ("ABA Routing Number", "021000021"), ("SWIFT/BIC", "CHASUS33"), ("Date", "October 29, 2026")], sign="Branch Manager")
    _doc(d / "certificate_of_insurance.pdf", issuer="Garden State Casualty", issuer_sub="Certificate of Liability Insurance",
         title="Certificate of Liability Insurance", accent=colors.HexColor("#3c4a5c"),
         rows=[("Named Insured", "Brightline Logistics Inc"), ("Policy Number", "GSC-TR-20931"), ("Coverage", "Motor Truck Cargo / General Liability"),
               ("Effective Date", "2026-09-20"), ("Expiry Date", "2027-12-31")], sign="Authorized Representative")
    _doc(d / "certificate_of_incorporation.pdf", issuer="State of New Jersey", issuer_sub="Department of the Treasury - Division of Revenue",
         title="Certificate of Incorporation", accent=colors.HexColor("#22314f"),
         rows=[("Company Name", "Brightline Logistics Inc"), ("Registration Number", "0450918273"),
               ("Date of Incorporation", "2026-09-14")], sign="State Treasurer")
    return {
        "submission_id": "SUB-2026-1044", "submitted_at": "2026-10-30", "resubmission_of": None,
        "vendor_type": "company", "category": "logistics",
        "company": {"legal_name": "Brightline Logistics Inc", "trading_name": "", "registration_number": "0450918273",
                    "country": "US", "address": {"line1": "2200 Commerce Blvd", "city": "Newark", "state": "NJ", "postal_code": "07114"},
                    "website": "https://brightline-logistics.com"},
        "tax": {"tax_id": "84-2290471"},
        "contact": {"name": "Mark Delaney", "title": "Owner", "email": "mark.delaney.logistics@gmail.com", "phone": "+1 973 555 0110"},
        "bank": {"account_holder": "Brightline Logistics Inc", "bank_name": "JPMorgan Chase Bank, N.A.", "bank_country": "US", "currency": "USD",
                 "account_number": "483920117", "routing_number": "021000021", "swift": "CHASUS33"},
        "documents": [{"type": "tax_document", "file": "docs/brightline/w9.pdf"}, {"type": "bank_proof", "file": "docs/brightline/bank_letter.pdf"},
                      {"type": "insurance", "file": "docs/brightline/certificate_of_insurance.pdf"},
                      {"type": "incorporation", "file": "docs/brightline/certificate_of_incorporation.pdf"}],
    }


def meridian():
    d = DOCS / "meridian"
    _doc(d / "gst_certificate.pdf", issuer="Government of India", issuer_sub="Goods and Services Tax Network - Form GST REG-06",
         title="Registration Certificate (Form GST REG-06)", accent=colors.HexColor("#5b3a0e"),
         paras=["This is to certify that the person below has been registered under the Goods and Services Tax Act, 2017."],
         rows=[("GSTIN", "27AAMCM7314K1Z2"), ("Legal Name", "MERIDIAN AGRO EXPORTS PRIVATE LIMITED"),
               ("Constitution of Business", "Private Limited Company"),
               ("Address of Principal Place", "5th Floor, Nariman Bhavan, Nariman Point, Mumbai, Maharashtra, 400021"),
               ("Date of Liability", "12/03/2018"), ("Type of Registration", "Regular")], sign="Goods and Services Tax Network")
    _doc(d / "bank_letter.pdf", issuer="ICICI Bank Limited", issuer_sub="Nariman Point Branch, Mumbai 400021",
         title="Bank Account Confirmation", accent=colors.HexColor("#ae282e"),
         paras=["This is to confirm that the following current account is maintained with us."],
         rows=[("Account Title", "MERIDIAN AGRO EXPORTS PRIVATE LIMITED"), ("Account Number", "000405012345"),
               ("IFSC", "ICIC0000456"), ("SWIFT", "ICICINBB"), ("Date", "27/10/2026")], sign="Branch Manager")
    _doc(d / "certificate_of_incorporation.pdf", issuer="Ministry of Corporate Affairs", issuer_sub="Registrar of Companies, Mumbai - Government of India",
         title="Certificate of Incorporation", accent=colors.HexColor("#173f5f"),
         rows=[("Name of Company", "MERIDIAN AGRO EXPORTS PRIVATE LIMITED"), ("Corporate Identity Number", "U01100MH2018PTC309876"),
               ("Date of Incorporation", "02/02/2018")], sign="Registrar of Companies")
    return {
        "submission_id": "SUB-2026-1045", "submitted_at": "2026-10-31", "resubmission_of": None,
        "vendor_type": "company", "category": "goods",
        "company": {"legal_name": "Meridian Agro Exports Private Limited", "trading_name": "", "registration_number": "U01100MH2018PTC309876",
                    "country": "IN", "address": {"line1": "5th Floor, Nariman Bhavan, Nariman Point", "city": "Mumbai", "state": "Maharashtra", "postal_code": "400021"},
                    "website": "https://meridianagro.in"},
        "tax": {"tax_id": "29AAMCM7314K1Z2"},
        "contact": {"name": "Farah Sheikh", "title": "Finance Manager", "email": "farah.sheikh@meridianagro.in", "phone": "+91 22 5555 0123"},
        "bank": {"account_holder": "Meridian Agro Exports Pvt Ltd", "bank_name": "ICICI Bank", "bank_country": "IN", "currency": "INR",
                 "account_number": "000405012345", "ifsc": "ICIC0000456", "swift": "ICICINBB"},
        "documents": [{"type": "tax_document", "file": "docs/meridian/gst_certificate.pdf"}, {"type": "bank_proof", "file": "docs/meridian/bank_letter.pdf"},
                      {"type": "incorporation", "file": "docs/meridian/certificate_of_incorporation.pdf"}],
    }


def volkov():
    d = DOCS / "volkov"
    _doc(d / "vat_confirmation.pdf", issuer="Bundeszentralamt für Steuern", issuer_sub="Bestätigung der Umsatzsteuer-Identifikationsnummer (USt-IdNr)",
         title="VAT registration confirmation / Umsatzsteuer-Identifikationsnummer", accent=colors.HexColor("#2b2b2b"),
         rows=[("USt-IdNr", "DE284639177"), ("Firmenname", "Volkov Industrial Supplies GmbH"),
               ("Anschrift", "Hafenstraße 12, 20457 Hamburg"), ("Erteilt am", "2017-06-19")], sign="Bundeszentralamt für Steuern")
    _doc(d / "bank_letter.pdf", issuer="Commerzbank AG", issuer_sub="Firmenkunden - Ness 7-9, 20457 Hamburg",
         title="Bankbestätigung / Bank Confirmation", accent=colors.HexColor("#ffcc00"),
         rows=[("Kontoinhaber", "Volkov Industrial Supplies GmbH"), ("IBAN", "DE83 3704 0044 0817 2645 00"),
               ("BIC", "COBADEFFXXX"), ("Date", "2026-10-22")], sign="Commerzbank AG")
    _doc(d / "handelsregister.pdf", issuer="Amtsgericht Hamburg", issuer_sub="Handelsregister Abteilung B - Registergericht",
         title="Handelsregister extract (HRB)", accent=colors.HexColor("#4a4a4a"),
         rows=[("Firmenname", "Volkov Industrial Supplies GmbH"), ("Registernummer", "HRB 154872"),
               ("Eintragungsdatum", "2017-05-30"), ("Sitz", "Hamburg"), ("Geschäftsführer", "Dmitri Volkov")], sign="Registergericht")
    return {
        "submission_id": "SUB-2026-1046", "submitted_at": "2026-11-01", "resubmission_of": None,
        "vendor_type": "company", "category": "goods",
        "company": {"legal_name": "Volkov Industrial Supplies GmbH", "trading_name": "", "registration_number": "HRB 154872",
                    "country": "DE", "address": {"line1": "Hafenstraße 12", "city": "Hamburg", "state": "", "postal_code": "20457"},
                    "website": "https://www.volkov-industrial.de"},
        "tax": {"tax_id": "DE284639177"},
        "contact": {"name": "Anna Becker", "title": "Buchhaltung", "email": "a.becker@volkov-industrial.de", "phone": "+49 40 5555 0188"},
        "bank": {"account_holder": "Volkov Industrial Supplies GmbH", "bank_name": "Commerzbank AG", "bank_country": "DE", "currency": "EUR",
                 "iban": "DE83370400440817264500", "swift": "COBADEFFXXX"},
        "documents": [{"type": "tax_document", "file": "docs/volkov/vat_confirmation.pdf"}, {"type": "bank_proof", "file": "docs/volkov/bank_letter.pdf"},
                      {"type": "incorporation", "file": "docs/volkov/handelsregister.pdf"}],
    }


def main():
    subs = {
        "happy_us_northwind": northwind(),
        "happy_in_shakti": shakti(),
    }
    k1, k2 = kestrel()
    subs.update({"edge_parent_account_expired_insurance": k1, "edge_resubmission_kestrel": k2,
                 "edge_bank_account_in_use": brightline(), "edge_gstin_typo_state": meridian(),
                 "edge_sanctions_near_match": volkov()})
    for name, s in subs.items():
        (HERE / f"{name}.json").write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(subs)} submissions and documents under {HERE}")


if __name__ == "__main__":
    main()
