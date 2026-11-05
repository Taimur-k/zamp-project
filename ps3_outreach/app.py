"""PS-3 web app: prospect in -> researched, grounded draft out (for human review).

Run:  python ps3_outreach/app.py      (http://127.0.0.1:8003)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
for p in (str(ROOT), str(APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import Body, HTTPException  # noqa: E402

import drafting  # noqa: E402
import pipeline as P  # noqa: E402
from common.runstore import RunStore, start_run  # noqa: E402
from common.webapp import create_app  # noqa: E402

store = RunStore(str(APP_DIR / "runs.db"))
SCEN = {s["id"]: s for s in P.SCENARIOS}
app = create_app(
    title="Prospect → Draft",
    subtitle="Personalised outreach: public research, a ranked hook, a grounded draft — never auto-sent",
    app_dir=APP_DIR,
    store=store,
    plan=P.PLAN,
    scenarios=[{k: s[k] for k in ("id", "title", "description", "kind")} for s in P.SCENARIOS],
)


@app.get("/api/seller")
def seller():
    return P.load_seller()


@app.post("/api/scenarios/{sid}/run")
def run_scenario(sid: str):
    sc = SCEN.get(sid)
    if not sc:
        raise HTTPException(404, "unknown scenario")
    rid = start_run(store, P.pipeline, P.PLAN, dict(sc["input"]), f"{sc['input']['name']} · {sc['input']['company']}", sid)
    return {"run_id": rid}


@app.post("/api/prospect/run")
def run_custom(payload: dict = Body(...)):
    inp = {k: (payload.get(k) or "").strip() for k in ("name", "title", "company", "domain", "linkedin", "context", "wiki_title", "research_mode")}
    if not inp["name"] or not inp["company"] or not inp["title"]:
        raise HTTPException(400, "name, title and company are required")
    seller = payload.get("seller") or {}
    inp["seller"] = {k: (seller.get(k) or "").strip() for k in ("name", "one_liner", "short", "rep_name") if seller.get(k)}
    rid = start_run(store, P.pipeline, P.PLAN, inp, f"{inp['name']} · {inp['company']}", payload.get("scenario") or "custom")
    return {"run_id": rid}


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, payload: dict = Body(...)):
    """Approve / reject / save edits. Re-checks edited copy. Never sends anything."""
    r = store.get(run_id)
    if not r or not r.get("result") or not (r["result"].get("draft")):
        raise HTTPException(404, "no draft for this run")
    res = r["result"]
    action = payload.get("action")
    if action not in ("approve", "reject", "save"):
        raise HTTPException(400, "action must be approve | reject | save")
    d = res["draft"]
    subject = (payload.get("subject") or d["subject"]).strip()
    body = (payload.get("body") or d["body"]).strip()
    seller = res.get("seller") or P.load_seller()
    prospect = res["prospect"]
    cites = [{"title": c["title"], "text": c.get("text") or c["title"], "source": c["source"]} for c in d["citations"]]
    g = drafting.grounding_check({"body": body, "linkedin": d["linkedin"], "cites": cites}, company=prospect["company"], prospect=prospect, seller=seller,
                                 rep_context=(r.get("input") or {}).get("context") or "")
    problems = [{"sentence": x["sentence"], "status": x["status"], "missing": x["missing"]} for x in g["stripped"]]
    edited = body != d["body"] or subject != d["subject"]
    status = {"approve": "approved", "reject": "rejected", "save": "edited"}[action]
    if action == "approve" and problems:
        status = "edited"  # can't approve copy with unsupported claims
    res["review"] = {"status": status, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "edited": edited,
                     "subject": subject, "body": body, "problems": problems, "note": (payload.get("note") or "")[:500]}
    if edited:
        d["subject"], d["body"] = subject, body
    P.write_draft_files(run_id, prospect, prospect["company"], d["subject"], d["body"], d["linkedin"], d["citations"], res["decision"], res.get("flags", []), res["review"])
    store.update(run_id, result_json=res)
    return res["review"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="warning")
