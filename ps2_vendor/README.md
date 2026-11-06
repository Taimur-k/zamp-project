# PS-2 · Vendor onboarding: from submission to approval

A working process that takes a vendor's registration (form JSON plus attached PDFs or scans) and runs it through eight checks, one after another. It ends with one of four outcomes: **APPROVED**, **PENDING** (the vendor has to fix something), **MANUAL_REVIEW** (a person in compliance has to decide) or **REJECTED** (fraud, sanctions or something that can't be fixed). The reasoning behind the outcome is visible, and the process writes real outputs: an email to the vendor (`.eml`), an internal escalation email when needed, and a new row in the vendor master for approved vendors.

## Run it

```bash
pip install fastapi uvicorn python-multipart pdfplumber pypdfium2 reportlab pillow rapidocr_onnxruntime
python ps2_vendor/app.py            # -> http://localhost:8002   (or: cd ps2_vendor && python app.py)
python ps2_vendor/test_scenarios.py # headless: runs every scenario and checks the expected decisions
python ps2_vendor/samples/make_samples.py   # regenerates the sample PDFs + submissions (already committed)
```

No API key is needed. All decisions come from fixed rules, so the same input always gets the same result. If `ANTHROPIC_API_KEY` is set, Claude is used for two optional extras, and the run log says which engine did what:
- filling in a document field the rules couldn't find
- rewording the vendor email (every fact and requested item is kept)

The decision itself is always made by the rules.

**UI:**
- **New run**: one-click prepared scenarios, plus a custom submission editor. You can edit the JSON live and drag in your own PDFs or images.
- **Live view**: shows each stage as it runs, with its log and the data it extracted.
- **Result panel**:
  - the decision with a one-line reason
  - what the vendor has to provide, and why
  - internal notes for the reviewer
  - a preview of the email
  - every check, grouped by stage
  - the documents that were read, and how
  - links to the files written
- **Dashboard**: run history, outcome mix, auto-approval rate, most common blocker, number of fraud and compliance holds.

## Process map

```
Submission (form JSON + PDFs/scans)
  1 Intake & normalise ........ tidy up fields, standardise country names, find the attachments, link a resubmission to its earlier run
  2 Completeness .............. required fields + documents depend on country, bank country, vendor type, category
  3 Read documents & cross-check  PDF text or OCR (scans) -> work out the document type (wrong document?) -> pull out fields
                                 -> compare with the form: name, tax ID, account no., holder, insurance expiry,
                                    registration no., company age
  4 Tax ID validation ......... country format + checksum + consistency (state code, PAN type, certificate)
  5 Bank validation ........... IBAN mod-97, ABA checksum, IFSC/SWIFT, sort code vs IBAN, holder name vs legal entity
  6 Fraud/duplicate/sanctions . watch-list fuzzy match, account or tax ID already used by another vendor in the master,
                                 email domain, cross-border account, newly incorporated -> risk score
  7 Decision .................. any REJECT -> REJECTED; else any REVIEW -> MANUAL_REVIEW; else any VENDOR item -> PENDING; else APPROVED
  8 Notify & write outputs .... vendor email (.eml) in outbox/; internal escalation for review/reject;
                                 approved -> new row in data/vendor_master_approved.csv (idempotent);
                                 audit entry in data/audit_log.jsonl; PENDING -> reply-by date + follow-up schedule
  ── human in the loop (review.py) ──────────────────────────────────────────────────────────────
  9 Reviewer decision ......... MANUAL_REVIEW / REJECTED -> named reviewer: approve | request info (-> PENDING) | reject
                                 guardrails enforced server-side; vendor email + audit entry written
 10 Follow-up ................. open-case queue on the dashboard: who acts next, age, reply-by, reminders;
                                 one-click reminder email listing what is still outstanding
```

Every case ends as **approved, pending or rejected**, as the brief asks. `MANUAL_REVIEW` is a holding state that only a named human can close.

**Key design choice:** all eight stages run even after a problem is found. The vendor gets **one** email that lists everything to fix, so there's no back-and-forth over five rounds. Each finding is tagged with *who has to act*:

| Finding type | Who acts | Leads to |
|---|---|---|
| `vendor` | Vendor must correct or provide something | PENDING + email listing exactly what's needed and why |
| `review` | Ambiguous; a person in compliance decides | MANUAL_REVIEW + neutral "routine check" email to vendor + internal ticket |
| `reject` | Fraud, sanctions or embargo | REJECTED + polite decline that **doesn't reveal the reason** + internal fraud alert |
| `warn` | Risk signal, not a blocker on its own | Added to a risk score; at 40 or more, MANUAL_REVIEW |

## Rules

| Area | Rule | Result if it fails |
|---|---|---|
| Completeness | Required fields depend on country (state for US/IN), bank country (US: routing + account; IN: IFSC + account; GB: IBAN or sort code + account; EU: IBAN + SWIFT; SWIFT when the account is cross-border), and vendor type (registration no. for companies) | vendor |
| Completeness | Required documents: tax document and bank proof always; certificate of incorporation for companies; insurance for on-site/professional services, logistics and construction | vendor |
| Documents | The content must match the declared type (a W-9 uploaded as a bank letter gets caught) | vendor |
| Documents | Name on the tax, incorporation and insurance documents matches the legal name (≥85% after normalising) | vendor |
| Documents | Tax ID on the form appears on the tax certificate; if not, the certificate's number is captured | vendor (merged into the tax finding) |
| Documents | Account number/IBAN on the form appears on the bank document; account holder is taken from the document | vendor |
| Documents | Insurance not expired (a warning if it expires within 30 days); company registration no. matches the certificate | vendor |
| Documents | Company incorporated less than 180 days ago | warn +15 |
| Tax: US | EIN is 9 digits **and** its first two digits are a prefix the IRS actually assigns (e.g. 07, 08, 09, 17 are never issued) | vendor |
| Tax: GB | VAT is GB + 9 digits **and** passes the mod-97 weighted checksum (both the old and the "+55" scheme) | vendor |
| Tax: IN | GSTIN is 15 characters, **passes the check-character algorithm**, its state code matches the address state, the PAN inside it has an entity type that matches the vendor type (C = company, P = individual), and the PAN's 5th character is the first letter of the name (warning only) | vendor |
| Tax: DE/EU | VAT format per country; the German USt-IdNr check digit (ISO 7064 MOD 11,10) | vendor |
| Bank | IBAN length per country + mod-97; IBAN country = bank country; for UK IBANs, the sort code and account number inside the IBAN must match the ones on the form | vendor |
| Bank | ABA routing 3-7-1 checksum + Federal Reserve prefix; IFSC format; IFSC bank ↔ SWIFT bank ↔ bank name; SWIFT country = bank country | vendor |
| Bank | **Account holder** (from the form *and* the bank document) = legal entity after removing legal suffixes (Ltd/LLC/Pvt/GmbH…), ≥90%. A trading-name match only gets a warning. A personal-looking name or a different company gets a specific request | vendor |
| Bank | Account in a different country from the company | warn +20 |
| Risk | Watch list (mock, with aliases): exact normalised name match → reject; 85–99% → **review** (not auto-reject) | reject / review |
| Risk | Embargoed country (IR, KP, SY, CU) for the company or the bank | reject |
| Risk | Same bank account as a **different** vendor in the master. Accounts are compared in a standard form, so a UK IBAN and the same sort code + account number count as the same account | reject (fraud hold) |
| Risk | Same tax ID as a different vendor (could be a rename) / same company already on file | review |
| Risk | Free webmail contact +15; email domain different from website +20; no website +5 | warn → review at 40 or more |

## Human in the loop: reviewer decisions, follow-up, audit

The brief notes that "the follow-up is manual and the only audit trail is whatever's in someone's inbox". The process covers both.

**Reviewer decision** (result panel of any `MANUAL_REVIEW` or `REJECTED` run). The reviewer enters their name and a reason, then picks one action:

| Action | Result | Vendor gets |
|---|---|---|
| Approve | APPROVED, vendor record created (idempotent per case) | Approval email with supplier ID |
| Request info | PENDING, the reviewer's question is added to the open items | "Action needed" email listing it, with a reply-by date |
| Reject | REJECTED | Neutral decline (no reason disclosed) |

The guardrails are enforced by the API, not just by the buttons:
- An **exact sanctions match or an embargoed country can never be approved**; it can only be confirmed as rejected.
- **Approve is blocked while the vendor still owes something.** The reviewer has to use "Request info".
- A name and a reason of at least 15 characters are required.
- Each run is reviewed once, and concurrent clicks are serialised.
- A resubmission after "request info" **goes back to a reviewer** (`REVIEWER_FOLLOWUP`). The rules can't judge whether a human's question was answered, so the earlier question is never shown as "resolved" automatically.

**Follow-up queue** (dashboard, "Open cases"). This shows the latest run of every case still waiting on someone: the vendor (PENDING) or a reviewer (MANUAL_REVIEW). For a PENDING case, a reminder is due after 3 days of silence, escalation to the internal requester after 7, and the case is overdue after the reply-by date (10 days). "Send reminder now" writes a reminder `.eml` listing exactly what is still outstanding. A reminder is refused for a case that has been superseded by a resubmission.

**Audit log** (`data/audit_log.jsonl`, append-only). There is one line per automated decision, reviewer decision and reminder. Each line records who acted, the outcome before and after, the reason, the finding codes and the emails written. The result panel shows the full trail for the case across resubmissions.

API: `GET/POST /api/runs/{id}/review`, `GET /api/followups`, `POST /api/runs/{id}/remind`.

## Prepared scenarios (one click each in the UI)

| Scenario | What's tricky | Expected |
|---|---|---|
| **Northwind Analytics LLC** (US) | Clean happy path: W-9, bank letter, insurance, certificate of formation all agree | APPROVED, supplier ID created |
| **Shakti Precision** (IN) | The cancelled cheque is a **photo with no text layer**, so it's read by OCR. The bank holder is `SHAKTI PRECISION COMPONENTS PRIVATE LIMITED` and the legal name is `Shakti Precision Components Pvt. Ltd.`. This **must pass**, which shows the matching isn't naive | APPROVED |
| **Kestrel FM** (GB) | The form looks perfect: the holder is filled in as the company itself. Only the **bank letter** shows the account belongs to the parent, *Kestrel Group Holdings Ltd*. Insurance also expired in March | PENDING: one email with exactly 2 requests (including the option of a third-party payment authorisation) |
| **Kestrel resubmission** | `resubmission_of` links it to the earlier run. It brings a new bank letter in the company's name and renewed insurance | APPROVED, showing "2 resolved, 0 still open" |
| **Brightline Logistics** (US) | Every document is internally consistent, but the bank account is already on file for **Harborview Freight (V-1007)**: the classic payment-diversion pattern. It also has a gmail contact and is a 7-week-old company | REJECTED. The vendor gets a neutral decline. The internal alert says to call V-1007 on the phone number already on file |
| **Meridian Agro** (IN) | GSTIN `29AAMCM7314K1Z2` has a valid *format*, but the check character fails and state 29 (Karnataka) contradicts the Mumbai address. The GST certificate shows `27AAMCM7314K1Z2`, which is valid | PENDING: **one** combined request ("your certificate shows 27…, just confirm") instead of 3 separate errors |
| **Volkov Industrial Supplies GmbH** (DE) | 92% name match with a watch-listed Russian entity; everything else is clean | MANUAL_REVIEW. Not auto-rejected. The reviewer gets the evidence and what to verify; the vendor gets a neutral "routine check" email |

`test_scenarios.py` also runs some extra checks:
- the reviewer flow: Volkov approved by a reviewer; an exact sanctions match can't be approved; approve is blocked while documents are missing; request info → PENDING → reminder → resubmission goes back to review; the follow-up queue and overdue logic
- malformed document entries don't crash a run; a group company named as account holder isn't mistaken for a personal account
- a routing number with one digit wrong goes to PENDING
- an EIN with the unassigned prefix 07 goes to PENDING
- an exact sanctions name goes to REJECTED
- two missing documents give PENDING with both listed
- an embargoed country goes to REJECTED
- running an approved submission again re-uses the same supplier ID, with no duplicate row

## Assumptions

- The buyer is a fictional "Acme Industries". Emails are written as `.eml` files to `ps2_vendor/outbox/` instead of being sent; plugging in SMTP or Gmail is a one-line change.
- The vendor master (`data/vendor_master.csv`) and the watch list (`data/watchlist.csv`) are mock data. In production they would be the ERP's supplier table and a screening provider's API (OFAC/EU/UN/UK lists).
- Supported countries: US, GB, IN, and EU VAT formats (DE gets full checksum validation). Any other country is routed to manual review instead of being guessed.
- The "today" used for checking insurance expiry and company age is the server's real date.
- A UK VAT number is required. (In reality small UK suppliers can be below the VAT threshold; that would become a "declare not VAT-registered" option.)
- Paying a parent company is not automatically wrong, but it needs a signed third-party payment authorisation. That's why it's PENDING with that option offered, not REJECTED.
- Tax ID re-use by a different name is a review, not a reject: a rename or acquisition is legitimate and common. Bank account re-use by a different company is a reject, because that is where the money goes.
- Fuzzy sanctions matches are never auto-rejected. Auto-rejecting on a name similarity score would turn away legitimate vendors and still need a human to confirm.
- Neither rejected nor manual-review emails reveal the reason (avoiding tipping off).
- A fraud hold (bank account already used by another vendor) can be overturned by a reviewer. Shared accounts are sometimes legitimate (payment agents, factoring). The UI asks for independent verification first, and the reason is audited. Sanctions and embargo hard stops can't be overturned.

## What I'd build next

1. A vendor-facing portal with field-level validation as they type: most PENDINGs are typos the checksums catch instantly.
2. Real verification APIs: IRS TIN matching, HMRC VAT check / VIES, GSTN public search, bank account verification (e.g. penny-drop / Confirmation of Payee). They would sit alongside the offline checksums.
3. Send reminders on a scheduler instead of a button (the due logic already exists in `review.followups`), add real SMTP, and add reviewer authentication with roles. Today the reviewer name is typed in, which is fine for a demo but not for production; a fraud-hold override should also need a second approver.
4. Ongoing monitoring after approval: insurance renewal reminders, periodic re-screening against the watch list, and bank-change requests going through the same pipeline with phone call-back verification.
5. LLM extraction for any document layout, with the rules kept as the cross-checking layer.
