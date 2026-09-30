"""FastAPI control/status API for the Bot Runtime (Phase 9).

Dashboard NO modifica la lógica de trading — este módulo solo expone
BotRuntime (start/pause/stop/status) y datos de solo lectura (señales
recientes, riesgo del día). No ejecuta órdenes, no conecta un broker.

Every response is built from BotRuntime.to_status_dict() / recent_signals_dict(),
which are hand-built, JSON-safe dicts that never include secrets (no webhook
URLs, no tokens) — see signal_engine/runtime/engine.py.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config.runtime_settings import ServerSettings
from ..runtime.engine import BotRuntime, read_signal_log
from ..runtime.models import BotStatus

from ..live.movers import MoversService
from . import maintenance
from .backtest import STRATEGY_INFO, BacktestError, BacktestManager
from .lab import LabError, LabManager
from .practice import PracticeKey
from .replay import ReplayData, ReplayError
from .chart import build_chart_data

STATIC_DIR = Path(__file__).resolve().parent / "static"

_VALID_ACTIONS = {"start", "pause", "stop"}

# Endpoints reachable without an auth token even when auth is enabled: the
# health check (for load balancers / systemd / uptime probes) and the static
# UI shell itself (the browser then sends the token on the /api/* calls).
_AUTH_EXEMPT_PREFIXES = ("/api/health", "/static", "/favicon")
_AUTH_EXEMPT_EXACT = {"/", "/practica"}


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
               service=None, backtests: BacktestManager | None = None,
               movers: MoversService | None = None, restart_fn=None,
               supervised: bool | None = None, lab: LabManager | None = None) -> FastAPI:
    settings = settings or ServerSettings()
    app = FastAPI(title="Futures Signal Engine — Control Dashboard")
    app.state.settings = settings
    app.state.service = service
    app.state.started_at = time.time()
    backtests = backtests or BacktestManager(Path(__file__).resolve().parents[2])
    movers = movers or MoversService()
    replay = ReplayData(backtests.root)
    lab = lab or LabManager(backtests.root, python=backtests.python)
    practice = PracticeKey(Path(backtests.root) / "data_store" / "practice_key")
    app.state.practice = practice
    restart_fn = restart_fn or maintenance.schedule_restart
    # launchd runs run.sh, which execs Python: the parent is launchd itself
    # (pid 1) and KeepAlive brings the bot back after it exits. Anything else
    # (a manual run in a Terminal) must not be told to exit.
    if supervised is None:
        supervised = os.getppid() == 1 or os.environ.get("BOT_SUPERVISED") == "1"

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
            # the practice key (a friend) opens only the bar replay, read-only
            if token != settings.auth_token and \
                    not practice.allows(token, request.method, request.url.path):
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
            "market_read": runtime.ghost_market_read.get(symbol, []),
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

    @app.get("/api/ghost/decisions")
    def get_ghost_decisions(limit: int = 100) -> dict:
        """Phase 15: EVERY GHOST decision (APPROVED + NO_TRADE) with reasons /
        confluence / context. This is the honest audit trail — dashboard shows
        it so the user can see the filter working."""
        if runtime.ghost_log_path is None:
            return {"decisions": [], "persisted": False}
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
        path = Path(runtime.ghost_log_path)
        if not path.exists():
            return {"decisions": [], "persisted": True}
        import json as _json
        lines = path.read_text().splitlines()
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except ValueError:
                continue
        return {"decisions": out[::-1], "persisted": True}

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

    @app.get("/api/replay/bars")
    def replay_bars(symbol: str = "NQ", data: str = "DATABENTO", date: str = "",
                    tf: str = "5m", days_before: int = 3, days_after: int = 2,
                    at: str = "09:30") -> dict:
        """Historical bars around a day for the manual practice (bar replay)."""
        try:
            return replay.bars(symbol=symbol, data=data, date=date, tf=tf,
                               days_before=days_before, days_after=days_after, at=at)
        except ReplayError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/market/movers")
    def get_movers() -> dict:
        """Mega caps' % change today and their estimated pull on NQ, in points."""
        st = runtime.instrument_status.get("NQ")
        return movers.snapshot(getattr(st, "last_price", None))

    @app.get("/api/ideas")
    def get_ideas() -> dict:
        """Trade ideas (heads-up / confirmed / direct / cancelled) of the most
        recent trading day, each with its trade's live or final result."""
        board = getattr(runtime, "idea_board", None)
        if board is None:
            return {"enabled": False, "day": None, "ideas": []}
        ideas = list(board.ideas.values())
        day = max((i.day for i in ideas), default=None)
        trades = []
        if runtime.paper_tracker is not None:
            trades = list(runtime.paper_tracker.open_trades) + \
                list(runtime.paper_tracker.closed_trades)
        by_num = {}
        for t in trades:
            ctx = t.context or {}
            if ctx.get("idea_number") is not None and \
                    t.opened_at.tz_convert("America/New_York").date().isoformat() == day:
                by_num[(t.instrument, ctx["idea_number"])] = t.to_dict()
        out = []
        for i in sorted((i for i in ideas if i.day == day), key=lambda i: i.number):
            d = i.to_event()
            d["trade"] = by_num.get((i.symbol, i.number))
            out.append(d)
        return {"enabled": True, "day": day, "ideas": out}

    # -- Backtests (dashboard "Backtest" view) ------------------------------ #
    @app.get("/api/backtest/options")
    def backtest_options() -> dict:
        return {"datasets": backtests.datasets(), "strategies": backtests.strategies(),
                "strategy_info": {k: {"title": t, "text": d}
                                  for k, (t, d) in STRATEGY_INFO.items()}}

    @app.get("/api/backtest/status")
    def backtest_status() -> dict:
        return backtests.status()

    @app.post("/api/backtest/run")
    async def backtest_run(request: Request) -> dict:
        body = await request.json()
        try:
            return backtests.start(symbol=str(body.get("symbol", "NQ")),
                                   data=str(body.get("data", "")),
                                   start=str(body.get("start", "")),
                                   end=str(body.get("end", "")),
                                   strategy=str(body.get("strategy", "zones")))
        except BacktestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/backtest/cancel")
    def backtest_cancel() -> dict:
        return backtests.cancel()

    @app.get("/api/backtest/runs")
    def backtest_runs() -> dict:
        return {"runs": backtests.runs()}

    @app.get("/api/backtest/runs/{run_id}")
    def backtest_get(run_id: str) -> dict:
        try:
            return backtests.get(run_id)
        except BacktestError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/backtest/runs/{run_id}/report")
    def backtest_report(run_id: str, kind: str = "summary") -> dict:
        try:
            return {"text": maintenance.report(backtests.root, run_id, kind,
                                               python=backtests.python)}
        except maintenance.MaintenanceError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    # -- Strategy lab (dashboard "Backtest") --------------------------------- #
    @app.get("/api/lab/status")
    def lab_status() -> dict:
        return lab.status()

    @app.post("/api/lab/run")
    def lab_run(position: str = "5mnq", sessions: str = "rth", estudio: str = "todo",
                escala: str = "puntos") -> dict:
        try:
            return lab.start(position=position, sessions=sessions, estudio=estudio,
                             escala=escala)
        except LabError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # -- Remote maintenance (dashboard "Sistema"): update + restart --------- #
    def _restart_block() -> str | None:
        if not supervised:
            return "El bot no corre bajo launchd; reinícialo a mano en la Mac."
        if backtests.running():
            return "Hay un backtest corriendo: reiniciar lo cortaría. Espera a que termine."
        if lab.running():
            return "El laboratorio está corriendo: reiniciar lo cortaría. Espera a que termine."
        return None

    @app.get("/api/bots")
    def get_bots() -> dict:
        """The bot hub: GHOST and ORBIT side by side — state, what each one
        is looking at now, record per version — and the strategy catalogue."""
        from .bots import overview
        try:
            commit = maintenance.version(backtests.root)
        except Exception:  # noqa: BLE001 - the hub never fails over git
            commit = {}
        return overview(runtime, orbit_dir=getattr(app.state, "orbit_dir", None), commit=commit,
                        shadows_dir=getattr(app.state, "shadows_dir", None))

    @app.post("/api/bots/{bot}/variants/{variant_id}")
    def switch_variant(bot: str, variant_id: str, on: bool) -> dict:
        """Turn a parallel variant on or off. GHOST's start/stop at once (they
        run in this process); ORBIT's follow within its next poll (~30 s)."""
        from ..runtime import shadows as sh
        base = getattr(app.state, "shadows_dir", None) or sh.SHADOWS_DIR
        bot = bot.lower()
        if bot not in ("ghost", "orbit") or \
                variant_id not in {sp.id for sp in sh.load_specs(bot)}:
            raise HTTPException(status_code=404, detail="no existe esa variante")
        if bot == "ghost":
            sh.switch_ghost_shadow(runtime, variant_id, on, base)
        else:
            sh.set_on("orbit", variant_id, on, base)
        return {"bot": bot, "id": variant_id, "on": on}

    # -- Práctica para un amigo ---------------------------------------------- #
    @app.get("/api/practice/datasets")
    def practice_datasets() -> dict:
        """Only what the practice screen needs to list (the practice key can
        read this; the backtest options it would otherwise use cannot)."""
        return {"datasets": backtests.datasets()}

    @app.get("/api/practice/link")
    def practice_link() -> dict:
        key = practice.get()
        return {"active": bool(key), "path": f"/practica?token={key}" if key else None,
                "protected": settings.auth_enabled}

    @app.post("/api/practice/link")
    def practice_new_link() -> dict:
        # without the owner's token the whole dashboard is open: a friend with
        # network access would see everything, so no link until it is locked
        if not settings.auth_enabled:
            raise HTTPException(status_code=409, detail=(
                "El dashboard no tiene clave (DASHBOARD_TOKEN): cualquiera que llegue a él "
                "lo vería todo. Ponle clave primero."))
        return {"active": True, "path": f"/practica?token={practice.new()}", "protected": True}

    @app.post("/api/practice/revoke")
    def practice_revoke() -> dict:
        practice.revoke()
        return {"active": False, "path": None, "protected": settings.auth_enabled}

    @app.get("/api/system/version")
    def system_version() -> dict:
        return {**maintenance.version(backtests.root), "can_restart": _restart_block() is None,
                "restart_blocked": _restart_block()}

    @app.post("/api/system/update")
    def system_update(restart: bool = True) -> dict:
        try:
            res = maintenance.update(backtests.root, python=backtests.python)
        except (maintenance.MaintenanceError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        blocked = _restart_block()
        res["restarting"] = bool(restart and res["updated"] and not blocked)
        res["restart_blocked"] = blocked if restart and res["updated"] else None
        if res["restarting"]:
            restart_fn()
        return res

    @app.post("/api/system/restart")
    def system_restart() -> dict:
        blocked = _restart_block()
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)
        restart_fn()
        return {"restarting": True}

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/practica")
        def practice_page() -> FileResponse:
            """The same page in practice-only mode (the link given to a friend)."""
            return FileResponse(STATIC_DIR / "index.html")

    return app
