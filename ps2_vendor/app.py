"""PS-2 Vendor onboarding - web app.  Run:  python ps2_vendor/app.py   ->  http://localhost:8002"""
from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Body, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from common.runstore import RunStore, start_run  # noqa: E402
from common.webapp import create_app  # noqa: E402
from ps2_vendor import pipeline as P  # noqa: E402
from ps2_vendor import review as R  # noqa: E402

UPLOAD_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
UPLOAD_MAX_BYTES = 15 * 1024 * 1024

store = RunStore(str(APP_DIR / "runs.db"))

app = create_app(
    title="Vendor Onboarding",
    subtitle="Submission → checks → approved / pending / review / rejected, with the vendor email written for you",
    app_dir=APP_DIR,
    store=store,
    plan=P.PLAN,
    scenarios=P.SCENARIOS,
)


def _label(data: dict) -> str:
    name = (data.get("company") or {}).get("legal_name") or data.get("legal_name") or "Unnamed vendor"
    return name + (" (resubmission)" if data.get("resubmission_of") else "")


@app.post("/api/scenarios/{sid}/run")
def run_scenario(sid: str):
    try:
        data = P.load_scenario(sid)
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, f"unknown scenario {sid}")
    return {"run_id": start_run(store, P.pipeline, P.PLAN, data, _label(data), sid)}


@app.get("/api/scenarios/{sid}/input")
def scenario_input(sid: str):
    try:
        return P.load_scenario(sid)
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, f"unknown scenario {sid}")


@app.post("/api/submit")
async def submit(submission: str = Form(...), files: list[UploadFile] = File(default=[]), doc_types: str = Form("[]")):
    """Custom input: the form JSON plus optional uploaded documents.

    An uploaded file declared as type T replaces any document of type T in the JSON,
    so you can swap e.g. the bank letter without editing the JSON."""
    try:
        data = json.loads(submission)
        assert isinstance(data, dict)
    except (ValueError, AssertionError):
        raise HTTPException(400, "submission must be a JSON object")
    try:
        types = json.loads(doc_types or "[]")
    except ValueError:
        types = []
    docs = list(data.get("documents") or [])
    up_dir = APP_DIR / "uploads"
    up_dir.mkdir(exist_ok=True)
    for i, f in enumerate(files or []):
        if not f.filename:
            continue
        if Path(f.filename).suffix.lower() not in UPLOAD_TYPES:
            raise HTTPException(400, f"'{f.filename}': only PDF or image files are accepted")
        content = await f.read()
        if len(content) > UPLOAD_MAX_BYTES:
            raise HTTPException(400, f"'{f.filename}' is larger than {UPLOAD_MAX_BYTES // (1024 * 1024)} MB")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(f.filename).name)[-80:]
        dest = up_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
        dest.write_bytes(content)
        t = types[i] if i < len(types) and types[i] else "tax_document"
        docs = [d for d in docs if d.get("type") != t]
        docs.append({"type": t, "file": f"uploads/{dest.name}"})
    data["documents"] = docs
    return {"run_id": start_run(store, P.pipeline, P.PLAN, data, _label(data), "custom")}


@app.get("/api/runs/{run_id}/review")
def review_options(run_id: str):
    """Which reviewer actions are allowed on this run (and why the others aren't), plus the case's audit trail."""
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    case = (run.get("result") or {}).get("root_submission_id")
    return R.options(run) | {"audit": R.audit_trail(case) if case else []}


@app.post("/api/runs/{run_id}/review")
def review_decide(run_id: str, payload: dict = Body(...)):
    """A named reviewer closes a MANUAL_REVIEW / REJECTED case: approve, reject or request info."""
    try:
        run = R.resolve(store, run_id, payload.get("action", ""), payload.get("reviewer", ""), payload.get("note", ""),
                        payload.get("vendor_message", ""))
    except R.ReviewError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "decision": run["decision"], "summary": run["summary"]}


@app.get("/api/followups")
def followups():
    """Open cases waiting on the vendor or a reviewer, most urgent first."""
    return R.followups(store)


@app.post("/api/runs/{run_id}/remind")
def remind(run_id: str):
    try:
        return R.send_reminder(store, run_id)
    except R.ReviewError as e:
        raise HTTPException(400, str(e))


@app.get("/api/file/{path:path}")
def get_file(path: str):
    """Serve an output or sample file (emails, CSV, PDFs) - only from this app's folders."""
    p = (ROOT / path).resolve()
    allowed = [APP_DIR / d for d in ("outbox", "data", "samples", "uploads")]
    if not any(str(p).startswith(str(a.resolve())) for a in allowed) or not p.is_file():
        raise HTTPException(404, "not found")
    media = {".eml": "text/plain; charset=utf-8", ".csv": "text/plain; charset=utf-8", ".pdf": "application/pdf"}.get(p.suffix.lower())
    return FileResponse(p, media_type=media)


def _ensure_samples_and_warm():
    if not (APP_DIR / "samples" / "happy_us_northwind.json").exists():
        from ps2_vendor.samples import make_samples

        make_samples.main()

    def warm():  # OCR the scanned sample once so the live demo doesn't wait for the OCR model
        try:
            from ps2_vendor import docs

            docs.extract_text(APP_DIR / "samples" / "docs" / "shakti" / "cancelled_cheque_scan.pdf")
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=warm, daemon=True).start()


_ensure_samples_and_warm()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8002)
