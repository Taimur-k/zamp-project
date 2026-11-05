"""PS-1 · Invoice processing — web app.

Run:  python ps1_invoice/app.py      (then open http://localhost:8001)
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fastapi import File, Form, HTTPException, UploadFile  # noqa: E402

from common.runstore import RunStore, start_run  # noqa: E402
from common.webapp import create_app  # noqa: E402
import pipeline  # noqa: E402

SAMPLES = HERE / "samples"
UPLOADS = HERE / "uploads"

SCENARIOS = [
    {"id": "happy", "file": "01_northwind_INV-10611.pdf", "kind": "happy",
     "title": "Northwind: clean invoice",
     "description": "Native PDF with an explicit PO and separate sales tax. Matches PO and goods receipt exactly → auto-approve and post."},
    {"id": "scanned_vat", "file": "02_apex_scanned_APX-56102.pdf", "kind": "edge",
     "title": "Apex (UK): scanned, VAT-inclusive",
     "description": "Image-only scan (OCR), prices include 20% VAT, PO written 'P.O. 4500 0217', unit prices ~1.4% above PO. Must net out VAT before comparing → approve within tolerance."},
    {"id": "split_overbill", "file": "03_brightline_BL-2318.pdf", "kind": "edge",
     "title": "Brightline: split PO, over-billed",
     "description": "Second invoice against a PO that's already 70% billed. Bills 18 more loads but only 8 have been received → review, with a suggested partial approval."},
    {"id": "duplicate", "file": "04_northwind_INV10482-R_reminder.pdf", "kind": "edge",
     "title": "Northwind: disguised duplicate",
     "description": "'Payment reminder' re-sending a paid invoice under 'INV10482-R' instead of 'INV-10482', as a new file → reject as duplicate, tell the vendor it was paid."},
    {"id": "missing_fields", "file": "05_quantum_statement_no_number.pdf", "kind": "edge",
     "title": "Quantum Cloud: no invoice no., date or PO",
     "description": "Bundled 'statement of services'. Two open POs share the same amount; the process picks the right one from the period text but can't post without an invoice number → return to vendor."},
]

import threading  # noqa: E402

import extract  # noqa: E402

threading.Thread(target=extract.warm_up, daemon=True).start()  # load OCR models in the background

store = RunStore(str(HERE / "runs.db"))
app = create_app(
    title="Invoice Desk",
    subtitle="PS-1 · Vendor invoice → PO match → reasoned AP decision",
    app_dir=HERE,
    store=store,
    plan=pipeline.PLAN,
    scenarios=[{k: s[k] for k in ("id", "title", "description", "kind")} for s in SCENARIOS],
)


def _start(path: Path, filename: str, scenario: str, channel: str) -> str:
    return start_run(store, pipeline.run_pipeline, pipeline.PLAN,
                     {"path": str(path), "filename": filename, "channel": channel},
                     label=filename, scenario=scenario)


@app.post("/api/scenarios/{sid}/run")
def run_scenario(sid: str):
    s = next((s for s in SCENARIOS if s["id"] == sid), None)
    if not s:
        raise HTTPException(404, "unknown scenario")
    return {"run_id": _start(SAMPLES / s["file"], s["file"], s["id"], "AP inbox (prepared sample)")}


@app.post("/api/run")
async def run_upload(file: UploadFile = File(...), note: str = Form("")):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "file too large (15 MB max)")
    UPLOADS.mkdir(exist_ok=True)
    safe = "".join(c for c in (file.filename or "invoice.pdf") if c.isalnum() or c in "._- ")[:80] or "invoice.pdf"
    path = UPLOADS / f"{uuid.uuid4().hex[:8]}_{safe}"
    path.write_bytes(data)
    return {"run_id": _start(path, safe, "custom upload", "manual upload")}


@app.post("/api/reset-ledger")
def reset_ledger():
    pipeline.reset_ledger()
    return {"ok": True}


@app.get("/api/reference")
def reference():
    ref = pipeline.load_reference()
    return {
        "vendors": ref["vendors"],
        "pos": list(ref["pos"].values()),
        "ledger": [{k: v for k, v in r.items() if k != "line_qty_map"} for r in ref["ledger"]],
        "policy": pipeline.POLICY,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
