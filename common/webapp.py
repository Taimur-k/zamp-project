"""FastAPI scaffolding shared by the three apps.

Each app calls `create_app(...)` to get the standard routes:
  GET  /                    -> the app's index.html (UI shell + app-specific JS)
  GET  /common/*            -> shared shell.css / shell.js
  GET  /api/meta            -> app title, engine in use, stage plan, sample scenarios
  GET  /api/runs            -> run history (dashboard)
  GET  /api/runs/{id}       -> one run with full stage logs (live view polls this)
  DELETE /api/runs          -> clear history
The app then adds its own POST route(s) that call `common.runstore.start_run`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import llm
from .runstore import RunStore

COMMON_STATIC = Path(__file__).resolve().parent / "static"


def create_app(*, title: str, subtitle: str, app_dir: Path, store: RunStore, plan: list[tuple[str, str]], scenarios: list[dict]) -> FastAPI:
    app = FastAPI(title=title)
    app.mount("/common", StaticFiles(directory=COMMON_STATIC), name="common")
    static_dir = app_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/meta")
    def meta():
        return {
            "title": title,
            "subtitle": subtitle,
            "engine": llm.engine_name(),
            "llm": llm.available(),
            "plan": [{"key": k, "title": t} for k, t in plan],
            "scenarios": scenarios,
        }

    @app.get("/api/runs")
    def runs():
        return store.list()

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        r = store.get(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r

    @app.delete("/api/runs")
    def clear():
        store.delete_all()
        return {"ok": True}

    return app
