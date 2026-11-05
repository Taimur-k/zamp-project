"""Run store + stage runner shared by all three case-study apps.

A *run* is one execution of a pipeline on one input. It is made of ordered
*stages*; each stage has a status, a log, and structured data. Stages are
persisted to SQLite as they happen, so the UI can poll a run and render it
live, and the dashboard can show history across runs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

# Minimum visible duration of a stage, in seconds, so the live view is watchable.
# Set PACE=0 to run at full speed (used by tests).
PACE = float(os.environ.get("PACE", "0.45"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT,
                    finished_at TEXT,
                    label TEXT,
                    scenario TEXT,
                    status TEXT,          -- running | completed | error
                    decision TEXT,       -- app-specific outcome, e.g. APPROVE / PENDING
                    summary TEXT,
                    input_json TEXT,
                    stages_json TEXT,
                    result_json TEXT,
                    duration_ms INTEGER
                )"""
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL + relaxed fsync: stage updates are written many times per run; durability of
        # every intermediate log line doesn't matter, latency of the live view does.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writes -------------------------------------------------------
    def create(self, label: str, scenario: str, input_data: dict) -> str:
        run_id = uuid.uuid4().hex[:10]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, created_at, label, scenario, status, input_json, stages_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, _now(), label, scenario, "running", json.dumps(input_data, default=str), "[]"),
            )
        return run_id

    def update(self, run_id: str, **fields):
        if not fields:
            return
        for k in ("stages_json", "result_json", "input_json"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], default=str)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def delete_all(self):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM runs")

    # ---- reads --------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row, full: bool) -> dict:
        d = dict(r)
        d["stages"] = json.loads(d.pop("stages_json") or "[]")
        d["result"] = json.loads(d.pop("result_json") or "null")
        d["input"] = json.loads(d.pop("input_json") or "null")
        if not full:
            d["stages"] = [{"key": s["key"], "title": s["title"], "status": s["status"]} for s in d["stages"]]
            d.pop("input", None)
        return d

    def get(self, run_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row(r, True) if r else None

    def list(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r, False) for r in rows]


class StageHandle:
    """Handed to pipeline code inside `with run.stage(...) as st:`."""

    def __init__(self, run: "Run", stage: dict):
        self._run = run
        self._s = stage

    def log(self, msg: str, level: str = "info"):
        self._s["logs"].append({"t": round(time.time() - self._run.t0, 2), "level": level, "msg": msg})
        self._run.flush()

    def data(self, **kv):
        self._s["data"].update(kv)
        self._run.flush()

    def status(self, status: str, note: str | None = None):
        """Override final status: done | warn | fail | skipped."""
        self._s["final"] = status
        if note:
            self._s["note"] = note
        self._run.flush()

    def note(self, text: str):
        self._s["note"] = text
        self._run.flush()


class Run:
    def __init__(self, store: RunStore, run_id: str, plan: list[tuple[str, str]]):
        self.store = store
        self.id = run_id
        self.t0 = time.time()
        # Pre-declare the planned stages so the UI can show the whole path as "pending".
        self.stages: list[dict] = [
            {"key": k, "title": t, "status": "pending", "logs": [], "data": {}, "note": None, "ms": None}
            for k, t in plan
        ]
        self.flush()

    def _find(self, key: str, title: str | None) -> dict:
        for s in self.stages:
            if s["key"] == key:
                return s
        s = {"key": key, "title": title or key, "status": "pending", "logs": [], "data": {}, "note": None, "ms": None}
        self.stages.append(s)
        return s

    def flush(self):
        clean = [{k: v for k, v in s.items() if k != "final"} for s in self.stages]
        self.store.update(self.id, stages_json=clean)

    @contextmanager
    def stage(self, key: str, title: str | None = None):
        s = self._find(key, title)
        if title:
            s["title"] = title
        s["status"] = "running"
        started = time.time()
        self.flush()
        handle = StageHandle(self, s)
        try:
            yield handle
        except Exception as e:  # stage crashed: record and re-raise so the run is marked error
            s["status"] = "fail"
            s["note"] = f"{type(e).__name__}: {e}"
            s["ms"] = int((time.time() - started) * 1000)
            self.flush()
            raise
        elapsed = time.time() - started
        if elapsed < PACE:
            time.sleep(PACE - elapsed)
        s["status"] = s.pop("final", None) or "done"
        s["ms"] = int((time.time() - started) * 1000)
        self.flush()

    def skip(self, key: str, reason: str):
        s = self._find(key, None)
        s["status"] = "skipped"
        s["note"] = reason
        self.flush()

    def skip_remaining(self, reason: str):
        for s in self.stages:
            if s["status"] == "pending":
                s["status"] = "skipped"
                s["note"] = reason
        self.flush()


def start_run(
    store: RunStore,
    pipeline: Callable[[Run, dict], dict],
    plan: list[tuple[str, str]],
    input_data: dict,
    label: str,
    scenario: str = "custom",
    background: bool = True,
) -> str:
    """Create a run record and execute `pipeline(run, input_data)` (in a thread by default).

    The pipeline returns a dict that must include `decision` and `summary`;
    everything else is stored as the run's result for the UI to render.
    """
    run_id = store.create(label, scenario, input_data)

    def _exec():
        run = Run(store, run_id, plan)
        try:
            result = pipeline(run, input_data) or {}
            run.skip_remaining("Not needed for this outcome")
            store.update(
                run_id,
                status="completed",
                decision=result.get("decision"),
                summary=result.get("summary"),
                result_json=result,
                finished_at=_now(),
                duration_ms=int((time.time() - run.t0) * 1000),
            )
        except Exception as e:
            run.skip_remaining("Run aborted")
            store.update(
                run_id,
                status="error",
                decision="ERROR",
                summary=f"{type(e).__name__}: {e}",
                result_json={"error": str(e), "trace": traceback.format_exc()},
                finished_at=_now(),
                duration_ms=int((time.time() - run.t0) * 1000),
            )

    if background:
        threading.Thread(target=_exec, daemon=True).start()
    else:
        _exec()
    return run_id
