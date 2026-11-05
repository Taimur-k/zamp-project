"""Headless check of every prepared scenario + the guardrails that matter most.

    python ps3_outreach/test_scenarios.py

Runs offline: research is served from data/cache (RESEARCH_MODE=cache), stages
run at full speed (PACE=0) and the LLM is disabled so results are deterministic.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["PACE"] = "0"
os.environ["RESEARCH_MODE"] = "cache"
os.environ["DISABLE_LLM"] = "1"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import drafting  # noqa: E402
import pipeline as P  # noqa: E402
import signals as S  # noqa: E402
from common.runstore import RunStore, start_run  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def run_scenarios():
    store = RunStore(os.path.join(tempfile.mkdtemp(), "test.db"))
    print("Scenarios (cache-only research):")
    for sc in P.SCENARIOS:
        rid = start_run(store, P.pipeline, P.PLAN, dict(sc["input"]), sc["title"], sc["id"], background=False)
        r = store.get(rid)
        res = r["result"] or {}
        check(r["status"] == "completed" and r["decision"] == sc["expect"], f"{sc['id']}: {r['decision']} (expected {sc['expect']}) — {r['summary']}")
        if res.get("draft"):
            d = res["draft"]
            check(all(c["status"] != "fail" for c in res["checks"] if c["name"] != "Personalisation score"), f"{sc['id']}: no failed quality checks")
            check(not any(row["status"] in ("unsupported", "uncited claim") for row in res["grounding"]) or res["decision"] == "LOW_CONFIDENCE", f"{sc['id']}: every kept claim is grounded")
            check(not drafting.SENSITIVE_IN_DRAFT.search(d["body"]), f"{sc['id']}: no sensitive language in the draft")
            check(Path(HERE / res["files"]["eml"]).exists(), f"{sc['id']}: draft written to {res['files']['eml']} (unsent)")
        if sc["id"] == "amex-sensitive":
            hook = next(s for s in res["signals"] if s["role"] == "hook")
            check(hook["type"] != "sensitive" and res["tact_mode"], "amex: hook is not the layoff story and tact mode is on")
        if sc["id"] == "fedex-role-change":
            check(res["role_check"].get("successor") == "Jamere Jackson", f"fedex: successor identified ({res['role_check'].get('successor')})")
        if sc["id"] == "mercury-ambiguous":
            check(any("mercury.com" == c["domain"] for c in res["candidates"]), "mercury: fintech candidate offered with its domain")
        if sc["id"] == "upwind-ceo":
            check("Wiz" not in res["draft"]["body"], "upwind: doesn't claim Upwind acquired Wiz (that was Google)")


def unit_checks():
    print("Guardrail unit checks:")
    hook = {"title": "Acme raises $40M Series B", "text": "Acme raises $40M Series B", "source": "Reuters"}
    draft = {"body": "Hi Jo,\n\nCongrats on the $40M Series B [1]. I hear Acme also acquired Globex last month [1].\n\nAcme is hiring 30 AP clerks.", "linkedin": "", "cites": [hook]}
    g = drafting.grounding_check(draft, company="Acme", prospect={"name": "Jo Smith", "title": "CFO"}, seller={"name": "Ledgerline"}, rep_context="")
    check("$40M" in g["body"], "grounding keeps a claim its source supports")
    check("Globex" not in g["body"], "grounding strips a cited claim the source doesn't support")
    check("30 AP clerks" not in g["body"], "grounding strips an uncited numeric claim")
    check(S.classify_news({"title": "Acme to cut 5% of workforce"}, "Acme", [], "Jo Smith")["type"] == "sensitive", "layoff headline classified sensitive")
    check(S.classify_news({"title": "Acme CFO Jo Smith sells $3.2 million in stock"}, "Acme", [], "Jo Smith")["type"] == "personal", "insider stock sale classified personal")
    check(S.classify_news({"title": "Upwind fills vacuum left by Google's acquisition of Wiz"}, "Upwind", [], "")["type"] != "m_and_a", "someone else's acquisition isn't the company's M&A")
    check(S.persona_of("VP, Accounts Payable") == "finance_ops" and S.persona_of("Chief Financial Officer") == "cfo" and S.persona_of("Co-founder & CEO") == "ceo", "persona detection")


if __name__ == "__main__":
    run_scenarios()
    unit_checks()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")
