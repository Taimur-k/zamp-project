"""Human-in-the-loop for PS-2: reviewer decisions, the follow-up queue, and reminders.

The automated pipeline can end in MANUAL_REVIEW (a human must judge) or REJECTED
(fraud / sanctions hold). The brief asks for every submission to end as approved,
pending or rejected, so this module lets a named reviewer close those cases. It
also takes over the vendor chasing that is otherwise done by hand.

Guardrails (all enforced server-side, never only in the UI):
* Hard stops can't be overridden: an exact sanctions match or an embargoed country
  can only be rejected.
* "Approve" is blocked while the vendor still owes us something; use "request info".
* Every action needs a reviewer name and a written reason. Each case is reviewed
  once per run; to change the outcome, the vendor resubmits or the case is re-run.
* Every action is appended to the audit log and emails the vendor (.eml in outbox/).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

from . import pipeline as P

ACTIONS = {"approve": "APPROVED", "reject": "REJECTED", "request_info": "PENDING"}
REVIEWABLE = {"MANUAL_REVIEW", "REJECTED"}
HARD_STOPS = {"SANCTIONS_MATCH": "exact sanctions-list match", "EMBARGO_COMPANY": "embargoed country",
              "EMBARGO_BANK": "bank in an embargoed country"}
MIN_NOTE = 15
_LOCK = threading.Lock()


class ReviewError(ValueError):
    """The requested action isn't allowed; the message is shown to the reviewer as-is."""


# ---------------------------------------------------------------- reviewer decisions
def options(run: dict) -> dict:
    """Which reviewer actions are possible on this run, and why the others aren't."""
    res = run.get("result") or {}
    if run.get("status") != "completed":
        return {"reviewable": False, "reason": "Run has not completed.", "actions": {}}
    if res.get("review"):
        return {"reviewable": False, "reason": "Already decided by a reviewer.", "actions": {}}
    if run.get("decision") not in REVIEWABLE:
        return {"reviewable": False, "reason": f"{run.get('decision')} cases don't need a reviewer decision.", "actions": {}}
    issues = res.get("issues", [])
    hard = [HARD_STOPS[i["code"]] for i in issues if i["code"] in HARD_STOPS]
    vendor_open = [i["title"] for i in issues if i["severity"] == "vendor"]
    if hard:
        approve = f"Not allowed: {', '.join(hard)}. This cannot be overridden."
    elif vendor_open:
        approve = f"The vendor still owes {len(vendor_open)} item(s) ({'; '.join(vendor_open)}). Use 'Request info'."
    else:
        approve = None
    return {"reviewable": True, "reason": None,
            "actions": {"approve": approve, "reject": None,
                        "request_info": "Not allowed: hard stop." if hard else None}}


def resolve(store, run_id: str, action: str, reviewer: str, note: str, vendor_message: str = "") -> dict:
    """Apply a reviewer decision to a completed run. Returns the updated run."""
    reviewer, note, vendor_message = (reviewer or "").strip(), (note or "").strip(), (vendor_message or "").strip()
    if action not in ACTIONS:
        raise ReviewError(f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
    if len(reviewer) < 2:
        raise ReviewError("Enter the reviewer's name - every decision is recorded against a person.")
    if len(note) < MIN_NOTE:
        raise ReviewError(f"Write the reason for the decision (at least {MIN_NOTE} characters) - it goes into the audit log.")
    if action == "request_info" and len(vendor_message) < 10:
        raise ReviewError("Write what the vendor needs to send - this text goes into the email to them.")

    with _LOCK:  # two reviewers clicking at once must not both succeed
        run = store.get(run_id)
        if not run:
            raise ReviewError("Run not found.")
        opts = options(run)
        if not opts["reviewable"]:
            raise ReviewError(opts["reason"])
        blocked = opts["actions"].get(action)
        if blocked:
            raise ReviewError(blocked)

        res = run["result"]
        s = res.get("normalised_submission") or {}
        root = res.get("root_submission_id") or s.get("submission_id")
        new_decision = ACTIONS[action]
        today = date.today()
        vendor_id, items = None, []

        if action == "approve":
            vendor_id = P._append_approved(s, root, run_id)
            subject, body = P._vendor_email("APPROVED", s, [], vendor_id, today)
            res["vendor_id"] = vendor_id
            res["outputs"].append({"label": f"Vendor master record {vendor_id} (reviewer approval)", "path": P._rel(P.APPROVED_CSV)})
        elif action == "reject":
            subject, body = P._vendor_email("REJECTED", s, [], None, today)
        else:
            items = list(res.get("vendor_requests") or []) + [{
                "title": "Additional information requested by our compliance team", "request": vendor_message,
                "why": "We need it to complete the review of your registration."}]
            subject, body = P._vendor_email("PENDING", s, items, None, today)
            res["vendor_requests"] = items
            res["issues"].append({"code": "REVIEWER_REQUEST", "stage": "decision", "stage_title": "Reviewer decision",
                                  "severity": "vendor", "title": "Additional information requested by reviewer",
                                  "detail": vendor_message, "request": vendor_message, "why": items[-1]["why"],
                                  "internal": note, "points": 0})
            res["reply_by"] = (today + timedelta(days=P.REPLY_DAYS)).isoformat()

        P.OUTBOX.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}_{run_id}"
        email = P._write_eml(P.OUTBOX / f"{stamp}_review.eml", s.get("contact_email") or "unknown@vendor", subject, body)
        email["engine"] = "rules template"
        res["outputs"].append({"label": "Email to vendor after review (.eml)", "path": email["path"]})

        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        label = {"APPROVED": "Approved", "REJECTED": "Rejected", "PENDING": "Pending vendor"}[new_decision]
        headline = f"{label} by reviewer {reviewer} (was {run['decision'].replace('_', ' ').lower()}): {note}"
        res["review"] = {"action": action, "reviewer": reviewer, "note": note, "vendor_message": vendor_message or None,
                         "at": at, "previous_decision": run["decision"], "new_decision": new_decision, "email": email,
                         "overrode": [i["title"] for i in res.get("issues", []) if i["severity"] in ("review", "reject")]}
        res.setdefault("decision_history", []).append({"decision": new_decision, "by": reviewer, "at": at, "note": note})
        res["headline"] = headline
        P.audit({"event": "reviewer_decision", "run_id": run_id, "case_id": root, "submission_id": s.get("submission_id"),
                 "vendor": s.get("legal_name"), "decision": new_decision, "previous_decision": run["decision"], "by": reviewer,
                 "action": action, "note": note, "vendor_message": vendor_message or None, "vendor_id": vendor_id,
                 "emails": [email["path"]]})
        store.update(run_id, decision=new_decision, summary=headline, result_json=res)
        return store.get(run_id)


# ---------------------------------------------------------------- follow-up queue
def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def followups(store, now: datetime | None = None) -> list[dict]:
    """Open onboarding cases: the latest run of each case that is still waiting on someone.

    PENDING waits on the vendor (reminder at day 3, escalation at day 7, overdue after the
    reply-by date). MANUAL_REVIEW waits on a reviewer. Approved and rejected cases are closed."""
    now = now or datetime.now(timezone.utc)
    latest: dict[str, dict] = {}
    for r in store.list(limit=1000):  # newest first
        res = r.get("result") or {}
        if r.get("status") != "completed" or not res:
            continue
        case = res.get("root_submission_id") or (res.get("vendor") or {}).get("submission_id") or r["id"]
        latest.setdefault(case, r)
    out = []
    for case, r in latest.items():
        res = r["result"]
        if r["decision"] not in ("PENDING", "MANUAL_REVIEW"):
            continue
        since = _parse((res.get("review") or {}).get("at")) or _parse(r.get("finished_at")) or _parse(r.get("created_at")) or now
        reminders = res.get("reminders") or []
        last_contact = max([since] + [d for d in (_parse(x["at"]) for x in reminders) if d])
        age = (now - since).days
        quiet = (now - last_contact).days
        if r["decision"] == "MANUAL_REVIEW":
            waiting, status, tone = "reviewer", ("Waiting for reviewer" if age < 2 else f"Review overdue ({age} days)"), ("info" if age < 2 else "bad")
        else:
            waiting = "vendor"
            reply_by = res.get("reply_by")
            if reply_by and now.date() > date.fromisoformat(reply_by):
                status, tone = f"Overdue since {reply_by}: escalate to the requester", "bad"
            elif age >= P.ESCALATE_DAYS:
                status, tone = f"No reply in {age} days: escalate to the requester", "bad"
            elif quiet >= P.REMINDER_DAYS:
                status, tone = "Reminder due", "warn"
            else:
                status, tone = f"Waiting for vendor (reminder in {P.REMINDER_DAYS - quiet} day(s))", "info"
        out.append({"case_id": case, "run_id": r["id"], "vendor": (res.get("vendor") or {}).get("legal_name") or r["label"],
                    "decision": r["decision"], "waiting_on": waiting, "since": since.isoformat(timespec="seconds"),
                    "age_days": age, "reply_by": res.get("reply_by"), "reminders_sent": len(reminders),
                    "open_items": [q["title"] for q in res.get("vendor_requests") or []] if waiting == "vendor"
                    else [i["title"] for i in res.get("issues", []) if i["severity"] in ("review", "reject")],
                    "status": status, "tone": tone})
    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda x: (order[x["tone"]], -x["age_days"]))


def send_reminder(store, run_id: str) -> dict:
    """Email the vendor a reminder listing what is still outstanding. Only for the latest run of a PENDING case."""
    with _LOCK:
        run = store.get(run_id)
        if not run:
            raise ReviewError("Run not found.")
        open_ = {f["run_id"]: f for f in followups(store)}
        if run_id not in open_ or open_[run_id]["waiting_on"] != "vendor":
            raise ReviewError("Reminders go only to the latest run of a case that is waiting on the vendor "
                              "(this one is closed, superseded by a resubmission, or waiting on a reviewer).")
        res = run["result"]
        s = res.get("normalised_submission") or {}
        items = res.get("vendor_requests") or []
        n = len(res.get("reminders") or []) + 1
        first = (s.get("contact_name") or "there").split()[0]
        ref = s.get("submission_id")
        lines = "\n".join(f"{k}. {q['request']}" for k, q in enumerate(items, 1))
        subject = f"Reminder{'' if n == 1 else f' {n}'}: {len(items)} item(s) still needed for your supplier registration ({ref})"
        body = (f"Dear {first},\n\nA quick reminder: we can't set {s.get('legal_name')} up for payment until we receive the following:\n\n"
                f"{lines}\n\nPlease reply to this email quoting reference {ref}"
                + (f" by {date.fromisoformat(res['reply_by']):%d %B %Y}" if res.get("reply_by") else "") + ". "
                "If anything is unclear, just reply and we'll help.\n\nKind regards,\nSupplier Onboarding Team\n"
                f"{P.BUYER}\nsupplier-onboarding@acme-industries.example")
        P.OUTBOX.mkdir(parents=True, exist_ok=True)
        email = P._write_eml(P.OUTBOX / f"{datetime.now():%Y%m%d-%H%M%S}_{run_id}_reminder{n}.eml",
                             s.get("contact_email") or "unknown@vendor", subject, body)
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        res.setdefault("reminders", []).append({"n": n, "at": at, "email": email})
        res["outputs"].append({"label": f"Reminder {n} to vendor (.eml)", "path": email["path"]})
        P.audit({"event": "reminder_sent", "run_id": run_id, "case_id": res.get("root_submission_id"), "submission_id": ref,
                 "vendor": s.get("legal_name"), "decision": run["decision"], "by": "follow-up automation", "reminder": n,
                 "open_items": [q["title"] for q in items], "emails": [email["path"]]})
        store.update(run_id, result_json=res)
        return {"reminder": n, "email": email}


def audit_trail(case_id: str, limit: int = 200) -> list[dict]:
    """Every audit entry for one onboarding case, oldest first."""
    import json

    if not P.AUDIT_LOG.exists():
        return []
    out = []
    with P.AUDIT_LOG.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("case_id") == case_id:
                out.append(e)
    return out[-limit:]
