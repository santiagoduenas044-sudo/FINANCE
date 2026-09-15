"""FastAPI control/status API for the Bot Runtime (Phase 9).

Dashboard NO modifica la lógica de trading — este módulo solo expone
BotRuntime (start/pause/stop/status) y datos de solo lectura (señales
recientes, riesgo del día). No ejecuta órdenes, no conecta un broker.

Every response is built from BotRuntime.to_status_dict() / recent_signals_dict(),
which are hand-built, JSON-safe dicts that never include secrets (no webhook
URLs, no tokens) — see signal_engine/runtime/engine.py.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config.runtime_settings import ServerSettings
from ..runtime.engine import BotRuntime, read_signal_log
from ..runtime.models import BotStatus

from .chart import build_chart_data

STATIC_DIR = Path(__file__).resolve().parent / "static"

_VALID_ACTIONS = {"start", "pause", "stop"}

# Endpoints reachable without an auth token even when auth is enabled: the
# health check (for load balancers / systemd / uptime probes) and the static
# UI shell itself (the browser then sends the token on the /api/* calls).
_AUTH_EXEMPT_PREFIXES = ("/api/health", "/static", "/favicon")
_AUTH_EXEMPT_EXACT = {"/"}


def _is_auth_exempt(path: str) -> bool:
    return path in _AUTH_EXEMPT_EXACT or path.startswith(_AUTH_EXEMPT_PREFIXES)


def _extract_token(request: Request) -> str:
    """Accept the token via `Authorization: Bearer <token>` (preferred) or a
    `?token=` query param (so the dashboard link can carry it on a phone). This
    is a placeholder auth mechanism — a real deployment should sit behind TLS
    and, ideally, a reverse proxy doing proper auth."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token", "").strip()


def create_app(runtime: BotRuntime, settings: ServerSettings | None = None,
               service=None) -> FastAPI:
    settings = settings or ServerSettings()
    app = FastAPI(title="Futures Signal Engine — Control Dashboard")
    app.state.settings = settings
    app.state.service = service
    app.state.started_at = time.time()

    # -- CORS: only when explicitly configured (never a wildcard) ---------- #
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    # -- Auth placeholder: bearer token, enforced only when configured ----- #
    # Local mode ships with auth DISABLED (loopback only). Remote mode REQUIRES
    # a token (ServerSettings.validate()). The token value is read from the
    # environment — never hard-coded, never returned in any response.
    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        if settings.auth_enabled and not _is_auth_exempt(request.url.path):
            token = _extract_token(request)
            if token != settings.auth_token:
                return JSONResponse(status_code=401,
                                    content={"detail": "unauthorized: missing or invalid token"})
        return await call_next(request)

    @app.get("/api/status")
    def get_status() -> dict:
        return runtime.to_status_dict()

    @app.get("/api/signals/recent")
    def get_recent_signals(limit: int = 20) -> dict:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
        return {"signals": runtime.recent_signals_dict(limit=limit)}

    @app.get("/api/signals/history")
    def get_signal_history(limit: int = 50) -> dict:
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
        if runtime.signal_log_path is None:
            return {"signals": [], "persisted": False}
        return {"signals": read_signal_log(runtime.signal_log_path, limit=limit), "persisted": True}

    @app.get("/api/risk/today")
    def get_risk_today() -> dict:
        return {
            "trades_today": runtime.trades_today,
            "daily_realized_pnl": runtime.daily_realized_pnl,
        }

    @app.get("/api/health")
    def get_health() -> dict:
        """Unauthenticated, secret-free liveness/readiness probe for systemd,
        Docker, and uptime monitors. Reports enough to tell healthy from stuck
        without exposing any configuration secret."""
        return {
            "ok": True,
            "status": runtime.status.value,
            "connection": runtime.connection_state(),
            "uptime_seconds": round(time.time() - app.state.started_at, 1),
            "last_heartbeat_at": runtime.to_status_dict()["last_heartbeat_at"],
            "symbols": sorted((runtime.live_feeds or {}).keys()),
            "mode": settings.mode,
        }

    @app.get("/api/service")
    def get_service() -> dict:
        """Runtime-loop diagnostics (poll/dedup counters). Empty when the API is
        created without a BotService (e.g. unit tests)."""
        svc = app.state.service
        return svc.stats() if svc is not None else {"running": False, "attached": False}

    @app.get("/api/config")
    def get_config() -> dict:
        """Secret-free view of how the server is configured (mode, host/port,
        auth on/off). Never returns the token itself."""
        return settings.public_dict()

    @app.get("/api/symbols")
    def get_symbols() -> dict:
        """Symbols that actually have a data feed configured — this is what the
        chart selector offers, so it never advertises a feed that isn't there."""
        return {"symbols": sorted((runtime.live_feeds or {}).keys())}

    @app.get("/api/chart/{symbol}")
    def get_chart(symbol: str, limit: int = 200) -> dict:
        # Only symbols with a configured feed are chartable. Reject anything
        # else explicitly (never build analysis for an unknown symbol).
        if symbol not in (runtime.live_feeds or {}):
            raise HTTPException(status_code=404, detail=f"no data feed for symbol {symbol!r}")
        if limit < 10 or limit > 600:
            raise HTTPException(status_code=400, detail="limit must be between 10 and 600")
        return build_chart_data(runtime, symbol, limit=limit)

    @app.get("/api/ghost/market/{symbol}")
    def get_ghost_market(symbol: str) -> dict:
        """GHOST's structured market state for a symbol (regime, MTF, structure,
        liquidity, key levels) plus its latest decision. Empty when GHOST is off
        or no cycle has run for the symbol yet."""
        if symbol not in (runtime.live_feeds or {}):
            raise HTTPException(status_code=404, detail=f"unknown symbol {symbol!r}")
        return {
            "enabled": runtime.ghost is not None,
            "market_state": runtime.ghost_market_state.get(symbol),
            "last_decision": runtime.ghost_last_decision.get(symbol),
        }

    @app.get("/api/ghost/trades/active")
    def get_active_trades(symbol: str | None = None) -> dict:
        if runtime.paper_tracker is None:
            return {"trades": [], "enabled": False}
        trades = runtime.paper_tracker.active(symbol)
        return {"trades": [t.to_dict() for t in trades], "enabled": True}

    @app.get("/api/ghost/trades/history")
    def get_trade_history(limit: int = 50) -> dict:
        if runtime.paper_tracker is None:
            return {"trades": [], "enabled": False}
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
        return {"trades": [t.to_dict() for t in runtime.paper_tracker.history(limit)],
                "enabled": True}

    @app.get("/api/ghost/stats")
    def get_ghost_stats() -> dict:
        if runtime.paper_tracker is None:
            return {"enabled": False}
        return {"enabled": True, **runtime.paper_tracker.stats()}

    @app.get("/api/signals/detail/{signal_id}")
    def get_signal_detail(signal_id: str) -> dict:
        """Full detail for one signal by id. Looks in the persisted log first
        (survives restart) then the in-memory recent signals."""
        candidates: list[dict] = []
        if runtime.signal_log_path is not None:
            candidates.extend(read_signal_log(runtime.signal_log_path, limit=1000))
        candidates.extend(runtime.recent_signals_dict(limit=1000))
        for rec in candidates:
            if rec.get("signal_id") == signal_id:
                return rec
        raise HTTPException(status_code=404, detail=f"signal {signal_id!r} not found")

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
