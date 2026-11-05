"""Run every prepared scenario headlessly and check the decision.

    python ps1_invoice/test_scenarios.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["PACE"] = "0"
os.environ.setdefault("DISABLE_LLM", "1")  # test the deterministic path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from common.runstore import RunStore, start_run  # noqa: E402
import pipeline  # noqa: E402
from app import SCENARIOS  # noqa: E402

EXPECTED = {
    "happy": "APPROVE",
    "scanned_vat": "APPROVE",
    "split_overbill": "REVIEW",
    "duplicate": "REJECT",
    "missing_fields": "RETURN_TO_VENDOR",
}

pipeline.reset_ledger()
store = RunStore(os.path.join(tempfile.mkdtemp(), "t.db"))
ok = True
for s in SCENARIOS:
    rid = start_run(store, pipeline.run_pipeline, pipeline.PLAN, {"path": str(HERE / "samples" / s["file"]), "filename": s["file"]},
                    label=s["file"], scenario=s["id"], background=False)
    r = store.get(rid)
    got = r["decision"]
    passed = got == EXPECTED[s["id"]]
    ok &= passed
    print(f"{'PASS' if passed else 'FAIL'}  {s['id']:<16} expected {EXPECTED[s['id']]:<17} got {got:<17} {r['duration_ms']} ms")
    print(f"      {r['summary'][:220]}")
    if r["status"] == "error":
        print(r["result"]["trace"])

# Re-running the happy path after it was posted must now be caught as a duplicate.
s = SCENARIOS[0]
rid = start_run(store, pipeline.run_pipeline, pipeline.PLAN, {"path": str(HERE / "samples" / s["file"]), "filename": s["file"]},
                label=s["file"], scenario="happy-rerun", background=False)
got = store.get(rid)["decision"]
print(f"{'PASS' if got == 'REJECT' else 'FAIL'}  happy re-run      expected REJECT (duplicate)  got {got}")
ok &= got == "REJECT"
pipeline.reset_ledger()
print("\nALL PASS" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
