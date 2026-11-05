"""Generate the sample invoice PDFs used by the prepared scenarios.

Each vendor gets a deliberately different layout (label wording, date format,
PO reference style, tax treatment) - and one invoice is a *scanned image* with
no text layer, so the process must OCR it.

Run:  python make_samples.py
"""
from __future__ import annotations

import io
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent / "samples"
OUT.mkdir(exist_ok=True)


def money(v: float) -> str:
    return f"{v:,.2f}"


def table(c: canvas.Canvas, x: float, y: float, widths: list[float], header: list[str], rows: list[list[str]],
          head_fill=colors.HexColor("#1f2937"), font="Helvetica", size=9.5, align_right_from=2) -> float:
    """Draw a simple table; returns the y below it."""
    h = 18
    c.setFillColor(head_fill)
    c.rect(x, y - h, sum(widths), h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont({"Times-Roman": "Times-Bold"}.get(font, font + "-Bold"), size)
    cx = x
    for i, (w, t) in enumerate(zip(widths, header)):
        if i >= align_right_from:
            c.drawRightString(cx + w - 6, y - 12.5, t)
        else:
            c.drawString(cx + 6, y - 12.5, t)
        cx += w
    y -= h
    c.setFillColor(colors.black)
    c.setFont(font, size)
    for r in rows:
        cx = x
        for i, (w, t) in enumerate(zip(widths, r)):
            if i >= align_right_from:
                c.drawRightString(cx + w - 6, y - 13, t)
            else:
                c.drawString(cx + 6, y - 13, t)
            cx += w
        y -= h
        c.setStrokeColor(colors.HexColor("#d1d5db"))
        c.line(x, y, x + sum(widths), y)
    return y


# --------------------------------------------------------------------------- 1. happy path
def northwind_happy(path: Path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    W, H = LETTER
    c.setFillColor(colors.HexColor("#0b5cab"))
    c.rect(0, H - 90, W, 90, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, "NORTHWIND OFFICE SUPPLIES INC.")
    c.setFont("Helvetica", 9.5)
    c.drawString(40, H - 68, "1450 Commerce Park Dr, Columbus, OH 43219  |  EIN 12-3456789  |  ar@northwind-supplies.com")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 125, "INVOICE")
    c.setFont("Helvetica", 10)
    meta = [("Invoice No:", "INV-10611"), ("Invoice Date:", "October 18, 2026"), ("Payment Terms:", "Net 30"),
            ("PO Number:", "PO-4500-0198")]
    y = H - 150
    for k, v in meta:
        c.drawString(40, y, k)
        c.drawString(140, y, v)
        y -= 15
    c.setFont("Helvetica-Bold", 10)
    c.drawString(360, H - 150, "Bill To:")
    c.setFont("Helvetica", 10)
    for i, l in enumerate(["Halcyon Manufacturing Corp.", "Accounts Payable", "200 Harbor Way", "Boston, MA 02110"]):
        c.drawString(360, H - 165 - 14 * i, l)
    rows = [
        ["PAP-A4-80", "A4 copy paper 80gsm (case of 5 reams)", "80", "42.50", money(80 * 42.50)],
        ["TON-26A", "HP 26A black toner cartridge", "12", "89.00", money(12 * 89.00)],
        ["CHR-ERG-02", "Ergonomic mesh office chair", "6", "245.00", money(6 * 245.00)],
    ]
    y = table(c, 40, H - 240, [80, 250, 50, 70, 82], ["Item", "Description", "Qty", "Unit Price", "Amount"], rows, align_right_from=2)
    sub = 5938.00
    tax = round(sub * 0.0825, 2)
    y -= 20
    for k, v, bold in [("Subtotal", money(sub), False), ("Sales Tax (8.25%)", money(tax), False), ("Total Due (USD)", money(sub + tax), True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(440, y, k)
        c.drawRightString(572, y, v)
        y -= 17
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#4b5563"))
    c.drawString(40, 90, "Remit to: First Ohio Bank, ABA 044000037, Account ending 4421")
    c.drawString(40, 76, "Please reference the invoice number with your payment. Thank you for your business!")
    c.save()


# --------------------------------------------------------------------------- 2. scanned, VAT-inclusive
def apex_invoice_canvas() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    c.setFont("Times-Bold", 18)
    c.drawString(50, H - 60, "Apex Industrial Components Ltd")
    c.setFont("Times-Roman", 10.5)
    c.drawString(50, H - 76, "Unit 7, Riverside Trade Park, Sheffield S9 2RX, United Kingdom")
    c.drawString(50, H - 90, "VAT Reg No: GB 284 5591 07   |   accounts@apexindustrial.co.uk")
    c.setFont("Times-Bold", 22)
    c.drawRightString(W - 50, H - 60, "TAX INVOICE")
    c.setFont("Times-Roman", 11)
    rows_meta = [("Invoice:", "APX-56102"), ("Date:", "24/10/2026"), ("Your Order Ref:", "P.O. 4500 0217"), ("Account:", "HALC-001")]
    y = H - 130
    for k, v in rows_meta:
        c.drawString(W - 230, y, k)
        c.drawString(W - 140, y, v)
        y -= 16
    c.drawString(50, H - 130, "Invoice to:")
    for i, l in enumerate(["Halcyon Manufacturing Corp.", "200 Harbor Way", "Boston, MA 02110, USA"]):
        c.drawString(50, H - 146 - 15 * i, l)
    rows = [
        ["Hydraulic fitting 1/2in BSP", "200", "5.11", money(200 * 5.11)],
        ["Bearing 6204-2RS", "150", "3.78", money(150 * 3.78)],
        ["Steel mounting bracket 90deg", "100", "9.13", money(100 * 9.13)],
    ]
    y = table(c, 50, H - 240, [260, 60, 90, 85], ["Description", "Qty", "Price inc VAT", "Line Total"], rows,
              head_fill=colors.HexColor("#374151"), font="Times-Roman", size=11, align_right_from=1)
    total = 200 * 5.11 + 150 * 3.78 + 100 * 9.13
    vat = round(total / 6, 2)
    y -= 24
    c.setFont("Times-Bold", 12)
    c.drawRightString(W - 150, y, "Total (incl. VAT)")
    c.drawRightString(W - 55, y, "GBP " + money(total))
    y -= 18
    c.setFont("Times-Roman", 10.5)
    c.drawRightString(W - 150, y, "of which VAT @ 20%")
    c.drawRightString(W - 55, y, money(vat))
    c.setFont("Times-Italic", 10)
    c.drawString(50, 120, "All prices include VAT at 20%. Payment due within 45 days.")
    c.drawString(50, 105, "Bank: Barclays, Sort code 20-45-77, Account ending 8812")
    c.save()
    return buf.getvalue()


def apex_scanned(path: Path):
    """Render to an image, add scan artefacts, save as an image-only PDF (no text layer)."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    pdf = pdfium.PdfDocument(apex_invoice_canvas())
    img = pdf[0].render(scale=2.2).to_pil().convert("L")
    random.seed(7)
    img = img.rotate(0.7, resample=Image.BICUBIC, expand=False, fillcolor=255)
    px = img.load()
    w, h = img.size
    for _ in range(9000):  # speckle noise
        x, y = random.randrange(w), random.randrange(h)
        px[x, y] = random.choice([170, 200, 230])
    img = img.filter(ImageFilter.GaussianBlur(0.5))
    img = img.point(lambda v: int(v * 0.93 + 8))  # slightly grey paper
    img.save(path, "PDF", resolution=200)


# --------------------------------------------------------------------------- 3. split PO / over-billing
def brightline_split(path: Path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    W, H = LETTER
    c.setFont("Helvetica-Bold", 22)
    c.setFillColor(colors.HexColor("#b45309"))
    c.drawString(40, H - 60, "BRIGHTLINE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    c.drawString(40, H - 76, "Brightline Logistics LLC | 900 W Fulton Market, Chicago, IL 60607 | Tax ID 47-2098811")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 40, H - 60, "Invoice # BL-2318")
    c.setFont("Helvetica", 10)
    c.drawRightString(W - 40, H - 76, "Invoice Date 10/29/2026")
    c.drawRightString(W - 40, H - 90, "Customer PO: 4500-0175")
    c.drawRightString(W - 40, H - 104, "Terms: Net 30")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, H - 120, "Bill To")
    c.setFont("Helvetica", 10)
    c.drawString(40, H - 134, "Halcyon Manufacturing Corp. - 200 Harbor Way, Boston, MA 02110")
    c.drawString(40, H - 160, "Services: Chicago to Dallas pallet lanes, service period Oct 6 - Oct 27, 2026 (invoice 2 of PO)")
    rows = [
        ["Pallet shipment Chicago to Dallas", "18", "310.00", money(18 * 310)],
        ["Liftgate delivery service", "18", "35.00", money(18 * 35)],
    ]
    y = table(c, 40, H - 190, [300, 60, 85, 87], ["Service", "Units", "Rate", "Amount"], rows,
              head_fill=colors.HexColor("#92400e"), align_right_from=1)
    y -= 22
    c.setFont("Helvetica", 10)
    c.drawRightString(440, y, "Sales tax (freight exempt)")
    c.drawRightString(572, y, "0.00")
    y -= 18
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(440, y, "Amount Due USD")
    c.drawRightString(572, y, money(18 * 310 + 18 * 35))
    c.setFont("Helvetica", 9)
    c.drawString(40, 80, "ACH: Lakeshore Commercial Bank, account ending 3307. Questions: billing@brightlinelogistics.com")
    c.save()


# --------------------------------------------------------------------------- 4. disguised duplicate
def northwind_duplicate(path: Path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    W, H = LETTER
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 55, "Northwind Office Supplies")
    c.setFont("Helvetica", 9.5)
    c.drawString(40, H - 70, "1450 Commerce Park Dr, Columbus, OH 43219 | EIN 12-3456789")
    c.setFillColor(colors.HexColor("#c42b2b"))
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(W - 40, H - 55, "PAYMENT REMINDER - PLEASE PAY")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    meta = [("Invoice #", "INV10482-R"), ("Date", "Sep 14, 2026"), ("Customer PO", "PO-4500-0201"), ("Status", "OVERDUE")]
    y = H - 100
    for k, v in meta:
        c.drawString(40, y, k)
        c.drawString(130, y, v)
        y -= 15
    rows = [
        ["Ground coffee 1kg", "30", "18.90", money(30 * 18.90)],
        ["Paper cups (pack of 500)", "9", "24.50", money(9 * 24.50)],
        ["Assorted snack box", "10", "38.88", money(10 * 38.88)],
    ]
    y = table(c, 40, H - 180, [300, 60, 80, 92], ["Description", "Qty", "Price", "Amount"], rows,
              head_fill=colors.HexColor("#6b7280"), align_right_from=1)
    sub = 1176.30
    tax = round(sub * 0.0825, 2)
    y -= 20
    for k, v in [("Subtotal", money(sub)), ("Tax 8.25%", money(tax)), ("Balance Due", money(sub + tax))]:
        c.setFont("Helvetica-Bold" if k == "Balance Due" else "Helvetica", 10.5)
        c.drawRightString(440, y, k)
        c.drawRightString(572, y, v)
        y -= 16
    c.setFont("Helvetica", 9)
    c.drawString(40, 90, "Our records show this invoice as unpaid. Remit to account ending 4421.")
    c.save()


# --------------------------------------------------------------------------- 5. missing critical fields
def quantum_missing(path: Path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    W, H = LETTER
    c.setFont("Helvetica-Bold", 18)
    c.setFillColor(colors.HexColor("#6d28d9"))
    c.drawString(40, H - 60, "Quantum Cloud Services Inc.")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9.5)
    c.drawString(40, H - 76, "55 Second St, San Francisco, CA 94105 | finance@quantumcloud.io")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, H - 120, "Statement of Services")
    c.setFont("Helvetica", 10)
    c.drawString(40, H - 140, "Prepared for: Halcyon Manufacturing Corp. - IT Department")
    c.drawString(40, H - 154, "Engagement: Managed IT support (helpdesk, patching, endpoint monitoring)")
    rows = [["IT services as agreed - Q3 2026 (July - September)", "18,000.00"]]
    y = table(c, 40, H - 190, [400, 132], ["Description", "Amount"], rows, head_fill=colors.HexColor("#4c1d95"), align_right_from=1)
    y -= 22
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(440, y, "Total Amount Due")
    c.drawRightString(572, y, "$18,000.00")
    c.setFont("Helvetica", 9)
    c.drawString(40, 90, "Payment by wire to account ending 5520. Thank you!")
    c.save()


if __name__ == "__main__":
    northwind_happy(OUT / "01_northwind_INV-10611.pdf")
    apex_scanned(OUT / "02_apex_scanned_APX-56102.pdf")
    brightline_split(OUT / "03_brightline_BL-2318.pdf")
    northwind_duplicate(OUT / "04_northwind_INV10482-R_reminder.pdf")
    quantum_missing(OUT / "05_quantum_statement_no_number.pdf")
    for p in sorted(OUT.glob("*.pdf")):
        print(p.name, p.stat().st_size)
