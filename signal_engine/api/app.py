"""FastAPI control/status API for the Bot Runtime (Phase 9).

Dashboard NO modifica la lógica de trading — este módulo solo expone
BotRuntime (start/pause/stop/status) y datos de solo lectura (señales
recientes, riesgo del día). No ejecuta órdenes, no conecta un broker.

Every response is built from BotRuntime.to_status_dict() / recent_signals_dict(),
which are hand-built, JSON-safe dicts that never include secrets (no webhook
URLs, no tokens) — see signal_engine/runtime/engine.py.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..runtime.engine import BotRuntime
from ..runtime.models import BotStatus

STATIC_DIR = Path(__file__).resolve().parent / "static"

_VALID_ACTIONS = {"start", "pause", "stop"}


def create_app(runtime: BotRuntime) -> FastAPI:
    app = FastAPI(title="Futures Signal Engine — Control Dashboard")

    @app.get("/api/status")
    def get_status() -> dict:
        return runtime.to_status_dict()

    @app.get("/api/signals/recent")
    def get_recent_signals(limit: int = 20) -> dict:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
        return {"signals": runtime.recent_signals_dict(limit=limit)}

    @app.get("/api/risk/today")
    def get_risk_today() -> dict:
        return {
            "trades_today": runtime.trades_today,
            "daily_realized_pnl": runtime.daily_realized_pnl,
        }

    @app.post("/api/control/{action}")
    def control(action: str) -> dict:
        # Validate the action explicitly — never eval/exec, never anything
        # beyond the three known, safe lifecycle transitions. No order
        # execution is reachable from this endpoint or any other.
        if action not in _VALID_ACTIONS:
            raise HTTPException(status_code=400,
                                detail=f"unknown action {action!r}; must be one of {sorted(_VALID_ACTIONS)}")

        previous_status = runtime.status
        if action == "start":
            event = runtime.start()
        elif action == "pause":
            if previous_status is not BotStatus.ONLINE:
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot pause: bot is {previous_status.value}, not ONLINE")
            event = runtime.pause()
        else:  # stop
            event = runtime.stop()

        return {
            "action": action,
            "previous_status": previous_status.value,
            "new_status": runtime.status.value,
            "event": {"kind": event.kind, "timestamp": event.timestamp.isoformat(),
                     "detail": event.detail},
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app
