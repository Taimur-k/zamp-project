# PS-1 · Invoice Desk — from PDF to a reasoned AP decision

An invoice (PDF, scanned PDF or image) goes in. A decision comes out:
**Approve**, **Review**, **Return to vendor** or **Reject**. Every step in between is shown live.
Each decision is backed by a list of rule hits and a record of what the process did.

```
python ps1_invoice/app.py            # → http://localhost:8001
python ps1_invoice/test_scenarios.py # headless check of every prepared scenario
python ps1_invoice/make_samples.py   # regenerate the sample invoices
```

You don't need an API key. Extraction is deterministic: label patterns plus table detection, with offline OCR for scans.
If `ANTHROPIC_API_KEY` is set (in the environment or a `.env` file at the repo root), Claude reads the document instead. The validation and decision rules stay deterministic either way, so every decision can be audited and repeated.

## Process map

| # | Stage | What it does | Can trigger |
|---|-------|--------------|-------------|
| 1 | Receive invoice | Fingerprints the file (SHA-256) so a re-sent file is caught later | – |
| 2 | Read document | Uses the PDF text layer if there is one. Otherwise it renders the page and runs OCR (rapidocr, offline) and reports confidence. OCR output is cached by file fingerprint | Review (low OCR confidence), Return (unreadable) |
| 3 | Extract fields | Invoice no., date, PO references (any format: `PO-4500-0198`, `4500-0175`, `P.O. 4500 0217`), currency, subtotal/tax/total, tax-inclusive flag, line items, tax ID, remit-to bank account. Each field keeps the line it came from as evidence | – |
| 4 | Identify vendor | Matches against the vendor master on tax ID, name/alias and email domain. Checks vendor status. **Bank account on the invoice must match the master** | Reject (unknown or blocked vendor), Hold (bank details changed, tax ID mismatch = lookalike vendor) |
| 5 | Validate invoice | Required fields. Date read in the vendor's locale (UK = day-first; ambiguity is flagged). Not future-dated or stale. Re-adds the invoice: qty × price, lines = subtotal, net + tax = total, tax rate consistent. **Tax-inclusive invoices are converted to net** | Return (missing or broken data), Review |
| 6 | Match purchase order | Uses the printed PO and checks it exists, belongs to this vendor and is open. **No PO printed:** scores this vendor's POs on amount, line-text similarity and period (Q3 vs Q4) and only accepts a clear winner | Review (inferred PO needs confirmation, PO closed), Return (no PO determinable) |
| 7 | 3-way match | For each line: invoice vs PO line (SKU or description) vs goods/service receipt. Takes into account quantities **already billed on earlier invoices (split POs)**. Checks price variance against tolerance and cumulative billing against PO value. Works out what is payable now | Review (price over tolerance, over-billing, billed > received) |
| 8 | Duplicate check | Same file fingerprint; same or variant invoice number (`INV10482-R` ≈ `INV-10482`) at the same amount; same vendor + amount within 10 days under a new number | Reject (duplicate), Review (possible duplicate, number reused) |
| 9 | Decide | The most severe rule hit wins: reject > hold > return > review > approve. Gives a plain-English headline and the reasons | – |
| 10 | Take action | **Approve:** posts to the AP ledger and schedules the payment date from terms. **Review:** creates a task for the PO's buyer (or the fraud desk), with a suggested partial approval. **Return/Reject:** drafts a vendor email to the address in the vendor master, never the one on the invoice, and saves it to `outbox/`. Every run writes `outputs/<run>_decision.json` | – |

## Policy (in `pipeline.py → POLICY`)
- Unit price may exceed the PO by **≤ 2%**, and the total price variance must stay **≤ 50** in the invoice currency.
- Cumulative billing may exceed the PO value only within the same 2% price tolerance.
- Quantity billed (including earlier invoices) must be **≤ received** and **≤ ordered**. Under-billing is fine.
- Arithmetic rounding allowance is 0.05. Invoices older than 180 days get a review. The duplicate window is 10 days.

## Scenarios (one click each in the UI)

| Scenario | What makes it hard | Expected |
|---|---|---|
| **Happy:** Northwind INV-10611 | Clean native PDF with explicit PO and separate tax | **APPROVE**, posted, payment scheduled |
| **Edge:** Apex (UK) APX-56102 | Image-only scan (OCR, no spaces between words). Prices include 20% VAT. Date `24/10/2026`. PO printed as `P.O. 4500 0217`. Unit prices about +1.4% over PO | **APPROVE**, with the VAT netted out and the variance noted as within tolerance |
| **Edge:** Brightline BL-2318 | Second invoice on a PO that's already 70% billed. Bills 18 loads, but only 8 more have been received (36 received, 28 already billed). That takes it to 115% of the PO | **REVIEW** for the buyer, with a suggested partial approval of USD 2,760 (the received portion) |
| **Edge:** Northwind "INV10482-R" reminder | A paid invoice re-sent as a "payment reminder" with a mutated number and a different file | **REJECT** as a duplicate, plus an email to the vendor saying it was paid on 2026-09-18 |
| **Edge:** Quantum Cloud statement | No invoice number, date or PO, and a single bundled line. Two open POs have the identical amount (Q3 and Q4); the process picks Q3 from the period text | **RETURN TO VENDOR**: email asking for invoice no. + date and quoting PO-4500-0230 |
| Re-run the happy path | The file was already posted by the first run | **REJECT** (duplicate). Use **Reset demo ledger** to re-demo |

## Assumptions
- Our POs follow the `4500-NNNN` numbering (SAP-style). Vendors write them many ways, and the process normalises them.
- The goods-receipt / service-entry table counts as "received". Services use service entry sheets.
- The ledger seed (`data/invoice_ledger_seed.csv`) represents invoices already paid. Approvals from demo runs go to `data/ledger_posted.csv`.
- No email is actually sent: vendor emails are written as `.eml` files to `outbox/`. Payments are "scheduled" by writing the due date into the decision record.
- Any bank detail change is a payment hold, never auto-approved: it is the top AP fraud pattern.
- An inferred PO always goes to a human to confirm, even when the match is confident ("no PO, no auto-pay").

## What I'd build next
- An email-inbox trigger (IMAP / Gmail watch) instead of upload.
- Writing back to a real ERP (NetSuite / SAP) instead of CSVs.
- A buyer approval link inside the review task, and learning tolerances per vendor.
- Field-level confidence that sends low-confidence OCR numbers to a "verify this figure" UI.
- Credit notes (negative invoices) and multi-currency FX tolerance.
