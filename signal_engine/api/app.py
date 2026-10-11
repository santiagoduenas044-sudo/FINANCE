"""FastAPI control/status API for the Bot Runtime (Phase 9).

Dashboard NO modifica la lógica de trading — este módulo solo expone
BotRuntime (start/pause/stop/status) y datos de solo lectura (señales
recientes, riesgo del día). No ejecuta órdenes, no conecta un broker.

Every response is built from BotRuntime.to_status_dict() / recent_signals_dict(),
which are hand-built, JSON-safe dicts that never include secrets (no webhook
URLs, no tokens) — see signal_engine/runtime/engine.py.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config.runtime_settings import ServerSettings
from ..runtime.engine import BotRuntime, read_signal_log
from ..runtime.models import BotStatus

from ..live.movers import MoversService
from . import maintenance
from .backtest import STRATEGY_INFO, BacktestError, BacktestManager
from .lab import LabError, LabManager
from .accounts import COOKIE, SESSION_DAYS, AccountError, Accounts, Throttle
from .practice import PRACTICE_PATHS, PracticeKey, write_env_value
from .replay import ReplayData, ReplayError
from .chart import build_chart_data

STATIC_DIR = Path(__file__).resolve().parent / "static"

_VALID_ACTIONS = {"start", "pause", "stop"}

# Endpoints reachable without an auth token even when auth is enabled: the
# health check (for load balancers / systemd / uptime probes) and the static
# UI shell itself (the browser then sends the token on the /api/* calls).
_AUTH_EXEMPT_PREFIXES = ("/api/health", "/static", "/favicon")
_AUTH_EXEMPT_EXACT = {"/", "/practica", "/login", "/api/login", "/api/logout", "/api/me",
                      "/api/signup", "/manifest.webmanifest", "/sw.js"}
# what a read-only user (role viewer) may not even read
_OWNER_ONLY_GET = ("/api/practice/link", "/api/users", "/api/backup", "/api/discord/channels")
# what any signed-in user may change for themself (not only the owner)
_SELF_SERVICE_POST = ("/api/push/", "/api/practice/state")
# all a practice-only user (signed up from the link) can open
_PRACTICA_PATHS = PRACTICE_PATHS | {"/api/practice/state"}


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

    # -- Acceso: token del dueño, usuarios con contraseña o llave de práctica - #
    # Sin token y sin usuarios el dashboard queda abierto (modo local, como
    # siempre). Con cualquiera de los dos, todo /api/* pide identificarse:
    #   * el token (DASHBOARD_TOKEN, header Bearer o ?token=) = dueño;
    #   * la cookie de sesión de /login = el rol de ese usuario (owner/viewer);
    #   * la llave de práctica = solo las velas de la práctica.
    # El token nunca aparece en una respuesta.
    accounts = Accounts(Path(backtests.root))
    app.state.accounts = accounts
    throttle, throttle_all = Throttle(5, 900), Throttle(30, 900)
    # addresses that logged in fine lately: the global brake (many failures
    # from everywhere) must not lock the owner out of their own page
    trusted: dict[str, float] = {}

    def auth_on() -> bool:
        return settings.auth_enabled or accounts.has_users()

    def identity(request: Request) -> tuple[str, str, str] | None:
        """(role, name, via) of whoever made the request, or None."""
        token = _extract_token(request)
        if settings.auth_token and token and hmac.compare_digest(token, settings.auth_token):
            return ("owner", "dueño", "token")
        s = accounts.verify(request.cookies.get(COOKIE))
        return (s[1], s[0], "session") if s else None

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if not auth_on():
            # never public without a lock: Tailscale Funnel marks what comes from internet
            if request.headers.get("tailscale-funnel-request") and not path.startswith("/static"):
                return JSONResponse(status_code=403, content={"detail": (
                    "Esta página todavía no tiene usuarios: crea uno en la Mac antes de "
                    "publicarla (bash deploy/macos/ctl.sh web usuario NOMBRE).")})
            return await call_next(request)
        if not _is_auth_exempt(path):
            who = identity(request)
            if who is None:
                # the practice key (a friend) opens only the bar replay, read-only
                if not practice.allows(_extract_token(request), request.method, path):
                    return JSONResponse(status_code=401,
                                        content={"detail": "unauthorized: missing or invalid token"})
            elif who[0] == "practica" and path not in _PRACTICA_PATHS:
                return JSONResponse(status_code=403, content={
                    "detail": "Tu cuenta es para practicar: esto no está en tu cuenta."})
            elif who[0] != "owner" and ((request.method not in ("GET", "HEAD")
                                         and not path.startswith(_SELF_SERVICE_POST))
                                        or path.startswith(_OWNER_ONLY_GET)):
                return JSONResponse(status_code=403, content={
                    "detail": "Tu usuario es de solo lectura: esto lo hace el dueño."})
        return await call_next(request)

    def _https(request: Request) -> bool:
        host = (request.headers.get("host") or "").split(":")[0]
        return (request.url.scheme == "https" or host.endswith(".ts.net")
                or request.headers.get("x-forwarded-proto", "").lower() == "https")

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "?"))
        known = time.time() - trusted.get(ip, 0.0) < 30 * 86400
        if throttle.blocked(ip) or (throttle_all.blocked("*") and not known):
            raise HTTPException(status_code=429, detail=(
                "Demasiados intentos fallidos. Espera 15 minutos y vuelve a intentar."))
        name = str(body.get("user", "")).strip()
        role = accounts.check(name, str(body.get("password", "")))
        if role is None:
            throttle.fail(ip)
            throttle_all.fail("*")
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos.")
        throttle.ok(ip)
        trusted[ip] = time.time()
        resp = JSONResponse({"user": name, "role": role})
        resp.set_cookie(COOKIE, accounts.issue(name), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=_https(request), path="/")
        return resp

    signup_ip, signup_all = Throttle(3, 3600), Throttle(40, 86400)

    @app.get("/api/signup")
    def signup_state() -> dict:
        return {"open": accounts.has_users() and accounts.signup_open()}

    @app.post("/api/signup")
    async def signup(request: Request) -> JSONResponse:
        """Anyone with the link makes their own practice-only user."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "?"))
        if signup_ip.blocked(ip) or signup_all.blocked("*"):
            raise HTTPException(status_code=429, detail="Demasiadas cuentas nuevas. Intenta más tarde.")
        name = str(body.get("user", "")).strip()
        try:
            accounts.signup(name, str(body.get("password", "")))
        except AccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        signup_ip.fail(ip)
        signup_all.fail("*")
        resp = JSONResponse({"user": name, "role": "practica"})
        resp.set_cookie(COOKIE, accounts.issue(name), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=_https(request), path="/")
        return resp

    @app.post("/api/signup/settings")
    async def signup_settings(request: Request) -> dict:
        accounts.set_signup(bool((await request.json()).get("open")))
        return {"open": accounts.signup_open()}

    # each user's practice account (balance, trades, drawings), on the server
    practice_dir = Path(backtests.root) / "data_store" / "practice_users"

    def _practice_file(request: Request) -> Path:
        who = identity(request)
        name = who[1] if who else ("dueño" if not auth_on() else None)
        if name is None:
            raise HTTPException(status_code=401, detail="Entra con tu usuario para guardar tu práctica.")
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:40] or "_"
        return practice_dir / f"{safe}.json"

    @app.get("/api/practice/state")
    def practice_state_get(request: Request) -> dict:
        f = _practice_file(request)
        try:
            return {"state": json.loads(f.read_text())}
        except (OSError, ValueError):
            return {"state": {}}

    @app.post("/api/practice/state")
    async def practice_state_put(request: Request) -> dict:
        f = _practice_file(request)
        raw = await request.body()
        if len(raw) > 2_000_000:
            raise HTTPException(status_code=413, detail="La práctica guardada es demasiado grande.")
        try:
            state = json.loads(raw or b"{}").get("state")
        except ValueError:
            state = None
        if not isinstance(state, dict) or any(not str(k).startswith("pr.") for k in state):
            raise HTTPException(status_code=400, detail="Formato de práctica inválido.")
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(f)
        return {"ok": True}

    @app.post("/api/users/{name}/role")
    async def change_role(name: str, request: Request) -> dict:
        try:
            accounts.set_role(name, str((await request.json()).get("role", "")))
        except AccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"users": accounts.listing()}

    @app.post("/api/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me(request: Request) -> dict:
        who = identity(request)
        return {"auth": auth_on(), "users": accounts.has_users(),
                "user": who[1] if who else None, "role": who[0] if who else
                ("owner" if not auth_on() else None), "via": who[2] if who else None}

    # -- notificaciones push (la app instalada) ------------------------------ #
    from ..output.webpush import PushStore, send as push_send
    push = PushStore(Path(backtests.root))
    app.state.push = push

    def push_user(request: Request) -> str:
        who = identity(request)
        if who is None:
            if auth_on():
                raise HTTPException(status_code=401, detail="Entra con tu usuario primero.")
            return "dueño"
        if who[0] not in ("owner", "viewer"):
            raise HTTPException(status_code=403, detail="Las notificaciones de los bots son para owner y viewer.")
        return who[1]

    @app.get("/api/push")
    def push_status(request: Request) -> dict:
        user = push_user(request)
        mine = push.of(user)
        return {"key": push.vapid.public_key, "phones": len(mine),
                "endpoints": [m["endpoint"] for m in mine],
                "prefs": mine[0]["prefs"] if mine else None}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request) -> dict:
        user = push_user(request)
        body = await request.json()
        try:
            push.add(user, body.get("subscription") or {}, body.get("prefs"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "phones": len(push.of(user))}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request) -> dict:
        user = push_user(request)
        endpoint = (await request.json()).get("endpoint", "")
        if endpoint in {m["endpoint"] for m in push.of(user)}:
            push.remove(endpoint)
        return {"ok": True, "phones": len(push.of(user))}

    @app.post("/api/push/prefs")
    async def push_prefs(request: Request) -> dict:
        user = push_user(request)
        n = push.set_prefs(user, (await request.json()).get("prefs") or {})
        return {"ok": True, "phones": n}

    @app.post("/api/push/test")
    def push_test(request: Request) -> dict:
        user = push_user(request)
        n = push_send(push, {"title": "👻 GHOST", "body": "Así te van a llegar los avisos de tus set ups.",
                             "tag": "test", "url": "/"}, "trades", user=user)
        return {"sent": n}

    @app.get("/api/users")
    def list_users() -> dict:
        return {"users": accounts.listing(), "token": settings.auth_enabled,
                "signup_open": accounts.signup_open()}

    @app.post("/api/users")
    async def save_user(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        try:
            accounts.add(str(body.get("user", "")), str(body.get("password", "")),
                         str(body.get("role", "viewer")))
        except AccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"users": accounts.listing()}

    @app.post("/api/users/{name}/delete")
    def delete_user(name: str) -> dict:
        try:
            if not accounts.remove(name):
                raise HTTPException(status_code=404, detail="Ese usuario no existe.")
        except AccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"users": accounts.listing()}

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

    @app.get("/api/discord/commands")
    def discord_commands() -> dict:
        """Whether !record, !setups… can answer, and why not (never the token)."""
        from ..output.discord_bot import STATUS
        return dict(STATUS)

    @app.get("/api/discord/channels")
    def discord_channels() -> dict:
        """Which set ups have their own Discord channel (names, never webhooks)."""
        from ..output import discord_channels as dch
        return {**dch.status(), "sending": bool(getattr(app.state, "history_thread", None)
                                                 and app.state.history_thread.is_alive())}

    @app.post("/api/discord/channels")
    def discord_channels_create() -> dict:
        """The bot makes a channel (and its webhook) for every set up that is on."""
        from ..output import discord_channels as dch
        try:
            return dch.create_channels()
        except dch.ChannelError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/discord/channels/history")
    def discord_channels_history() -> dict:
        """Send each set up's past trades to its own channel (in the background)."""
        import threading
        from ..output import discord_channels as dch
        from .bots import trade_log
        th = getattr(app.state, "history_thread", None)
        if th is not None and th.is_alive():
            return {"started": False, "detail": "Ya se está enviando."}
        log = trade_log(runtime, orbit_dir=getattr(app.state, "orbit_dir", None),
                        shadows_dir=getattr(app.state, "shadows_dir", None))

        def run():
            try:
                dch.send_history(log)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("discord history failed")
        app.state.history_thread = threading.Thread(target=run, name="historial", daemon=True)
        app.state.history_thread.start()
        return {"started": True}

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
        try:
            return overview(runtime, orbit_dir=getattr(app.state, "orbit_dir", None),
                            commit=commit, shadows_dir=getattr(app.state, "shadows_dir", None),
                            lab_dir=lab.dir)
        except Exception as exc:  # noqa: BLE001 - say what broke instead of a bare 500
            logging.getLogger(__name__).exception("bot hub failed")
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    @app.get("/api/bots/trades")
    def get_bot_trades() -> dict:
        """Every closed trade of every bot, version and parallel variant
        (the calendar and the history)."""
        from .bots import trade_log
        try:
            return trade_log(runtime, orbit_dir=getattr(app.state, "orbit_dir", None),
                             shadows_dir=getattr(app.state, "shadows_dir", None))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception("trade log failed")
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    @app.get("/api/backup")
    def get_backup() -> dict:
        """Where the daily copies of the data go and the newest one (owner)."""
        from . import backup
        return backup.status()

    @app.get("/api/backup.zip")
    def get_backup_zip() -> Response:
        """Everything in data_store/ right now, as a zip to keep (owner)."""
        from . import backup
        day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        return Response(backup.zip_bytes(Path(backtests.root) / "data_store"),
                        media_type="application/zip", headers={
                            "Content-Disposition": f'attachment; filename="ghost-respaldo-{day}.zip"',
                            "Cache-Control": "no-store"})

    @app.get("/api/record.pdf")
    def get_record_pdf(setup: int | None = None) -> Response:
        """The record of every set up (or just `setup`) as a PDF to download."""
        from ..output.record_pdf import build_pdf
        from .bots import trade_log
        try:
            log = trade_log(runtime, orbit_dir=getattr(app.state, "orbit_dir", None),
                            shadows_dir=getattr(app.state, "shadows_dir", None))
            pdf = build_pdf(log, only=setup)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception("record pdf failed")
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        name = f"record-set-up-{setup}" if setup else "record-set-ups"
        day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        return Response(pdf, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{name}-{day}.pdf"',
            "Cache-Control": "no-store"})

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
        spec = next(sp for sp in sh.load_specs(bot) if sp.id == variant_id)
        if getattr(spec, "retired", False):
            raise HTTPException(status_code=409, detail=f"{spec.label} está retirado: ya no corre.")
        was = sh.is_on(bot, spec, base)
        if bot == "ghost":
            sh.switch_ghost_shadow(runtime, variant_id, on, base)
        else:
            sh.set_on("orbit", variant_id, on, base)
        if was != bool(on) and sh.alerts_on():
            _say_switch(bot, spec, bool(on))
        return {"bot": bot, "id": variant_id, "on": on}

    def _say_switch(bot: str, spec, on: bool) -> None:
        """"🟢 Set up 3 · FVG 1 DÍA encendido" in the bot's Discord channel."""
        try:
            if bot == "ghost":
                disp = getattr(runtime, "output_dispatcher", None)
                publish = disp.publish_note if disp is not None else None
            else:
                from ..subbots.orbit import make_publisher
                publish = make_publisher()
            if publish is None:
                return
            publish(title=f"{'🟢' if on else '⚪'} {spec.label} {'encendido' if on else 'apagado'}",
                    text=(f"{spec.about}\nCorre en paralelo, en papel, y avisa aquí cada "
                          "entrada y salida." if on else
                          "Ya no opera ni avisa. Su récord se guarda; se puede volver a "
                          "encender desde la página (Bots)."),
                    color=0x3498DB if on else 0x95A5A6,
                    key=f"switch:{bot}:{spec.id}:{int(time.time())}")
        except Exception as exc:  # noqa: BLE001 - a message never blocks the switch
            logging.getLogger(__name__).warning("switch note failed (%s)", type(exc).__name__)

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
                "protected": auth_on()}

    @app.post("/api/practice/link")
    def practice_new_link() -> dict:
        # without the owner's token the whole dashboard is open: a friend with
        # network access would see everything, so no link until it is locked
        if not auth_on():
            raise HTTPException(status_code=409, detail=(
                "El dashboard no tiene clave (DASHBOARD_TOKEN) ni usuarios: cualquiera que "
                "llegue a él lo vería todo. Ponle clave o crea un usuario primero."))
        return {"active": True, "path": f"/practica?token={practice.new()}", "protected": True}

    @app.post("/api/system/lock")
    def system_lock() -> dict:
        """Give the dashboard its own key when it has none (from the phone,
        no Terminal): saved in .env for the next starts and active at once.
        Once there is a key this answers only to it, and refuses to change it."""
        if settings.auth_enabled:
            raise HTTPException(status_code=409, detail="El dashboard ya tiene clave.")
        import secrets
        token = secrets.token_urlsafe(18)
        try:
            write_env_value(Path(backtests.root) / ".env", settings.auth_token_env, token)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"No pude guardar la clave ({exc}).")
        os.environ[settings.auth_token_env] = token
        return {"token": token}

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

        # the pages are always asked to the Mac (no stale copy after an update,
        # and the app shows "sin conexión" instead of an empty page)
        fresh = {"Cache-Control": "no-cache"}

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html", headers=fresh)

        @app.get("/login")
        def login_page() -> FileResponse:
            return FileResponse(STATIC_DIR / "login.html", headers=fresh)

        @app.get("/practica")
        def practice_page() -> FileResponse:
            """The same page in practice-only mode (the link given to a friend)."""
            return FileResponse(STATIC_DIR / "index.html", headers=fresh)

        # installable as an app (home screen, full screen): the manifest and the
        # service worker live at the root so they cover the whole site
        @app.get("/manifest.webmanifest")
        def manifest() -> FileResponse:
            return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

        @app.get("/sw.js")
        def service_worker() -> FileResponse:
            return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                                headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    return app
