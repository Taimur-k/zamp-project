"""Headless test: run every prepared scenario end-to-end and assert the decision.

Run:  python ps2_vendor/test_scenarios.py
Uses a temporary run DB, outbox and approved-vendor CSV so it never touches demo data.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

os.environ["PACE"] = "0"
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR.parent))

from common.runstore import RunStore, start_run  # noqa: E402
from ps2_vendor import pipeline as P  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ps2_test_"))
    P.APPROVED_CSV = tmp / "vendor_master_approved.csv"
    P.OUTBOX = tmp / "outbox"
    store = RunStore(str(tmp / "runs.db"))
    failures = 0

    def run(data, sid):
        rid = start_run(store, P.pipeline, P.PLAN, data, sid, sid, background=False)
        return store.get(rid)

    def check(cond, msg):
        nonlocal failures
        print(("  ok   " if cond else "  FAIL ") + msg)
        failures += 0 if cond else 1

    results = {}
    for sc in P.SCENARIOS:
        r = run(P.load_scenario(sc["id"]), sc["id"])
        results[sc["id"]] = r
        res = r["result"] or {}
        print(f"\n{sc['id']}: {r['decision']} (expected {sc['expected']}) in {r['duration_ms']} ms")
        print(f"  {r['summary']}")
        if r["status"] == "error":
            print(res.get("trace"))
        for i in res.get("issues", []):
            print(f"    [{i['severity']}] {i['code']}: {i['detail']}")
        check(r["decision"] == sc["expected"], f"decision {r['decision']} == {sc['expected']}")
        check(all(s["status"] != "pending" for s in r["stages"]), "every stage reached a final state")
        if res.get("email"):
            check((APP_DIR.parent / res["email"]["path"]).exists() or Path(res["email"]["path"]).exists(), "vendor .eml written")

    # specific behaviours
    print("\nspecific behaviours:")
    k = results["edge_parent_account_expired_insurance"]["result"]
    codes = {i["code"] for i in k["issues"]}
    check(codes == {"BANK_HOLDER_MISMATCH", "INSURANCE_EXPIRED"}, f"Kestrel asks for exactly 2 things: {sorted(codes)}")
    rs = results["edge_resubmission_kestrel"]["result"]["resubmission"]
    check(bool(rs) and len(rs["resolved"]) == 2 and not rs["still_open"], f"resubmission linked and resolved both items: {rs}")
    sh = results["happy_in_shakti"]["result"]
    check(any("OCR" in d["method"] for d in sh["documents"]), "Shakti cheque read via OCR")
    check(not any(i["code"].startswith("BANK_HOLDER") for i in sh["issues"]), "'PRIVATE LIMITED' vs 'Pvt. Ltd.' not flagged")
    m = results["edge_gstin_typo_state"]["result"]
    check([i["code"] for i in m["issues"]] == ["TAXID_INVALID"] and "27AAMCM7314K1Z2" in m["issues"][0]["request"],
          "Meridian: one consolidated GSTIN request that quotes the certificate's number")
    b = results["edge_bank_account_in_use"]["result"]
    check("V-1007" in b["issues"][0]["detail"] and "unable to proceed" in b["email"]["body"] and "V-1007" not in b["email"]["body"],
          "Brightline: fraud hold names V-1007 internally, vendor email does not")
    check(b.get("internal_email") is not None, "Brightline: internal escalation email written")
    v = results["edge_sanctions_near_match"]["result"]
    check("Volkov" not in v["email"]["body"].split("registration")[0] or "watch" not in v["email"]["body"].lower(),
          "Volkov: vendor email does not mention the watch-list")

    # idempotency: same approved submission again must not create a second vendor record
    again = run(P.load_scenario("happy_us_northwind"), "happy_us_northwind")
    first_id = results["happy_us_northwind"]["result"]["vendor_id"]
    check(again["decision"] == "APPROVED" and again["result"]["vendor_id"] == first_id, f"re-run reuses vendor {first_id}, no duplicate")
    lines = P.APPROVED_CSV.read_text(encoding="utf-8").strip().splitlines()
    check(len(lines) == 1 + 3, f"approved CSV has 3 vendors (Northwind, Shakti, Kestrel): {len(lines) - 1}")

    # live edits a reviewer might make in the custom form (same submission_id = same onboarding case)
    def dec(d, msg, want):
        r = run(d, "custom")
        check(r["decision"] == want, f"{msg} -> {r['decision']} (want {want})")
        return r

    d = P.load_scenario("happy_us_northwind")
    d["bank"]["routing_number"] = "121000247"
    dec(d, "one-digit routing typo (ABA checksum)", "PENDING")
    d = P.load_scenario("happy_us_northwind")
    d["tax"]["tax_id"] = "07-3829105"
    dec(d, "EIN with unassigned prefix 07", "PENDING")
    d = copy.deepcopy(P.load_scenario("happy_us_northwind"))
    d["company"]["legal_name"] = "Orient Star Shipping Co"
    dec(d, "exact watch-list name", "REJECTED")
    d = P.load_scenario("happy_in_shakti")
    d["documents"] = d["documents"][:1]
    r = dec(d, "two missing documents", "PENDING")
    check(len(r["result"]["vendor_requests"]) == 2, "  ...and the email lists both")
    d = P.load_scenario("happy_us_northwind")
    d["company"]["country"] = "Iran"
    dec(d, "embargoed country", "REJECTED")
    d = P.load_scenario("happy_us_northwind")
    d["submission_id"] = "SUB-NEW-CASE"
    dec(d, "same vendor submitted again as a NEW case (duplicate onboarding)", "MANUAL_REVIEW")
    d = P.load_scenario("happy_us_northwind")
    d["bank"]["account_holder"] = "Priya Raman"
    r = dec(d, "account holder is the contact person", "PENDING")
    check(any(i["code"] == "BANK_HOLDER_PERSONAL" for i in r["result"]["issues"]), "  ...flagged as a personal account")
    d = P.load_scenario("happy_us_northwind")
    d["documents"][1]["type"] = "insurance"
    d["documents"][2]["type"] = "bank_proof"
    dec(d, "bank letter and insurance certificate swapped", "PENDING")

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILURE(S)'}  (temp dir {tmp})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
