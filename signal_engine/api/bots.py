"""The dashboard's bot hub: every bot's state, what it is looking at right now,
its record (today, total and per version) and the strategy catalogue.

GHOST runs inside this process, so it is read from the runtime. ORBIT is a
separate process: it is read from the files it saves every poll (its state
file doubles as its heartbeat).
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from ..output.record import commission_per_trade
from ..versions import BOT_VERSIONS, STRATEGIES, version_by_date

NY = "America/New_York"
ORBIT_ALIVE_SECONDS = 180          # ORBIT saves its state every 30 s
_REASONS_ES = {
    "active_trade": "ya había otro trade abierto", "cooldown": "pausa tras un stop",
    "too_late_in_session": "después de la hora límite", "invalid_session": "fuera de horario",
    "excessive_volatility": "volatilidad excesiva", "stale_data": "datos atrasados",
    "duplicate": "repetida", "conflicting_setups": "señales opuestas",
    "insufficient_rr": "poca ganancia para el riesgo", "target_too_close": "objetivo muy cerca",
    "setup_incomplete": "la señal no estaba completa", "stop_too_tight": "stop muy corto",
    "stop_too_wide": "stop muy largo", "risk_rejected": "control de riesgo",
    "daily_limit": "límite del día", "insufficient_confluence": "poca confluencia",
}


def _px(v) -> str:
    return "—" if v is None else f"{float(v):,.2f}"


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.0f}"


def _ny(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize(NY) if t.tzinfo is None else t.tz_convert(NY)


def _record(usd: list[float]) -> dict:
    n = len(usd)
    wins = sum(1 for u in usd if u > 0)
    return {"n": n, "wins": wins, "win_rate": (wins / n) if n else None,
            "net_usd": round(sum(usd), 2), "avg_usd": round(sum(usd) / n, 2) if n else None,
            "won_usd": round(sum(u for u in usd if u > 0), 2),
            "lost_usd": round(sum(u for u in usd if u < 0), 2)}


def _versions(bot: str, rows: list[tuple[str, float]]) -> list[dict]:
    """rows: (version, $) per closed trade -> one line per version, newest first."""
    out = []
    for vid, start, title, rules in BOT_VERSIONS[bot]:
        out.append({"version": vid, "since": start, "title": title, "rules": rules,
                    **_record([u for v, u in rows if v == vid])})
    out[-1]["current"] = True
    return out[::-1]


# --------------------------------------------------------------------------- #
# GHOST
# --------------------------------------------------------------------------- #
def ghost_card(runtime, now: pd.Timestamp) -> dict:
    feeds = list(getattr(runtime, "live_feeds", None) or {}) or ["NQ"]
    sym = "NQ" if "NQ" in feeds else feeds[0]
    try:
        pos = runtime._point_values(sym) or {}
    except Exception:  # noqa: BLE001
        pos = {}
    label, pv = next(iter(pos.items())) if pos else (f"1 punto {sym}", 1.0)
    fee = commission_per_trade(label)
    status = getattr(getattr(runtime, "status", None), "value", "OFFLINE")
    tracker = getattr(runtime, "paper_tracker", None)
    cfg = runtime.data_engine.cfg
    zr = cfg.setups.zone_retrace

    lines: list[dict] = []
    open_t = [t for t in (tracker.active(sym) if tracker is not None else [])]
    for t in open_t:
        side = "Compra" if t.direction == "LONG" else "Venta"
        lines.append({"kind": "trade", "text":
                      f"{side} abierta en {_px(t.entry)} · stop {_px(t.stop)} · objetivo "
                      f"{_px(t.tp2 if t.tp2 is not None else t.tp1)} · va "
                      f"{t.pnl_points:+.2f} pts ({_usd(t.pnl_points * pv)})"})
    board = getattr(runtime, "idea_board", None)
    watching = [i for i in (board.ideas.values() if board is not None else [])
                if i.status == "WATCHING" and i.symbol == sym]
    for i in sorted(watching, key=lambda i: i.number)[-3:]:
        side = "compra" if i.direction == "LONG" else "venta"
        lines.append({"kind": "watch", "text":
                      f"Vigilando la idea #{i.number}: {side} en la zona {_px(i.bottom)}–"
                      f"{_px(i.top)} (entra solo si una vela de 15 min reacciona ahí)."})
    for text in (getattr(runtime, "ghost_market_read", {}) or {}).get(sym, [])[:4]:
        lines.append({"kind": "read", "text": text})
    last = (getattr(runtime, "ghost_last_decision", {}) or {}).get(sym)
    if last and last.get("timestamp"):
        when = _ny(last["timestamp"]).strftime("%d-%b %H:%M")
        if last.get("action") in ("LONG", "SHORT"):
            text = f"Última idea ({when}): {'compra' if last['action'] == 'LONG' else 'venta'} tomada."
        else:
            why = " · ".join(_REASONS_ES.get(r, r.replace("_", " "))
                             for r in last.get("no_trade_reasons") or []) or "sin motivo"
            text = f"Última idea ({when}): descartada — {why}."
        lines.append({"kind": "decision", "text": text})

    inst = (getattr(runtime, "instrument_status", {}) or {}).get(sym)
    session = getattr(inst, "session", None)
    hm = now.strftime("%H:%M")
    in_window = now.dayofweek < 5 and "09:30" <= hm < (cfg.ghost.last_entry_time or "15:30")
    if status != "ONLINE":
        state = "paused" if status == "PAUSED" else "off"
    elif open_t:
        state = "trading"
    elif watching:
        state = "watching"
    elif in_window and session == "RTH":
        state = "scanning"
    else:
        state = "closed"
    if not lines:
        lines.append({"kind": "read", "text":
                      "Busca huecos de precio de 15 min frescos; avisa cuando el precio se acerca."
                      if state == "scanning" else
                      "Fuera de horario: entra de 9:30 a 15:30 ET y cierra todo a las 16:55."})

    closed = [t for t in (tracker.closed_trades if tracker is not None else [])
              if t.closed_at is not None and t.instrument == sym]
    today = now.date()
    rows = [((t.context or {}).get("bot_version") or version_by_date("GHOST", t.opened_at),
             t.pnl_points * pv) for t in closed]
    return {
        "name": "GHOST", "color": "gold", "strategy": f"Zonas {zr.timeframe}", "position": label,
        "status": status, "state": state, "symbol": sym,
        "heartbeat": _iso(getattr(runtime, "last_heartbeat_at", None)),
        "fee_per_trade": fee,
        "now": lines,
        "today": _record([t.pnl_points * pv for t in closed if _ny(t.closed_at).date() == today]),
        "total": _record([u for _, u in rows]),
        "open_usd": round(sum(t.pnl_points for t in open_t) * pv, 2) if open_t else None,
        "versions": _versions("GHOST", rows),
    }


# --------------------------------------------------------------------------- #
# ORBIT
# --------------------------------------------------------------------------- #
def orbit_card(store_dir: Path | None, now: pd.Timestamp) -> dict:
    from ..subbots.opening_range import OpeningRangeBot
    from ..subbots.orbit import STORE_DIR, bot_name, settings
    store_dir = Path(store_dir or STORE_DIR)
    s = settings()
    base = {"name": bot_name(), "color": "violet", "strategy": "APERTURA",
            "position": s.position, "fee_per_trade": commission_per_trade(s.position)}
    state_file = store_dir / "apertura_state.json"
    if not state_file.exists():
        return {**base, "status": "OFFLINE", "state": "off", "heartbeat": None,
                "now": [{"kind": "read", "text":
                         "ORBIT todavía no ha corrido en esta Mac (bash deploy/macos/ctl.sh orbit on)."}],
                "today": _record([]), "total": _record([]), "open_usd": None,
                "versions": _versions("ORBIT", [])}
    age = time.time() - state_file.stat().st_mtime
    alive = age <= ORBIT_ALIVE_SECONDS
    bot = OpeningRangeBot(store_dir, settings=s, name=base["name"])
    st, t = bot.st, bot.st.trade
    today = now.date().isoformat()
    lines = []
    open_usd = None
    if t is not None:
        long = t["long"]
        px = st.last_close
        pts = None if px is None else (px - t["entry"]) if long else (t["entry"] - px)
        if pts is not None:
            open_usd = round(pts * s.usd_per_point * s.contracts, 2)
        lines.append({"kind": "trade", "text":
                      f"{'Compra' if long else 'Venta'} abierta en {_px(t['entry'])} · stop "
                      f"{_px(t['stop'])} · objetivo {_px(t['target'])}"
                      + (f" · va {pts:+.2f} pts ({_usd(open_usd)})" if pts is not None else "")})
        state = "trading"
    elif st.day == today and st.box_sent and not st.done and st.hi is not None:
        w = st.hi - st.lo
        lines.append({"kind": "watch", "text":
                      f"Caja lista {_px(st.lo)}–{_px(st.hi)} ({w:.1f} pts). Espera que una vela "
                      f"de 5 min cierre fuera (hasta las {s.last_entry})."})
        state = "watching"
    elif st.day == today and not st.box_sent and st.hi is not None:
        lines.append({"kind": "watch", "text":
                      f"Armando la caja de 9:30 a 9:45: máximo {_px(st.hi)}, mínimo {_px(st.lo)}."})
        state = "scanning"
    elif st.day == today and st.done:
        done_today = [c for c in bot.closed if c.get("day") == today]
        if done_today:
            c = done_today[-1]
            lines.append({"kind": "decision", "text":
                          f"Hoy ya operó: {c.get('why', 'cerrado')} {c.get('pts', 0):+.2f} pts "
                          f"({_usd(c.get('usd', 0))}). Un trade al día."})
        else:
            lines.append({"kind": "decision", "text":
                          "Hoy no opera (la caja salió demasiado grande o no hubo ruptura)."})
        state = "done"
    else:
        lines.append({"kind": "read", "text": "Espera la apertura: arma la caja de 9:30 a 9:45 ET."})
        state = "closed"
    if st.last_ts:
        lines.append({"kind": "read", "text":
                      f"Última vela leída: {_ny(st.last_ts).strftime('%d-%b %H:%M')} ET"
                      + (f" a {_px(st.last_close)}." if st.last_close is not None else ".")})
    if not alive:
        state = "stale"
        lines.insert(0, {"kind": "alert", "text":
                         f"Sin señal de vida hace {age / 60:.0f} min: revisa con "
                         "bash deploy/macos/ctl.sh orbit status."})
    rows = [(c.get("version") or ("v2" if c.get("symbol") == "NQ" else "v1"),
             float(c.get("usd", 0))) for c in bot.closed]
    return {**base, "status": "ONLINE" if alive else "STALE", "state": state,
            "heartbeat": pd.Timestamp(state_file.stat().st_mtime, unit="s", tz="UTC").isoformat(),
            "now": lines,
            "today": _record([float(c.get("usd", 0)) for c in bot.closed if c.get("day") == today]),
            "total": _record([u for _, u in rows]), "open_usd": open_usd,
            "versions": _versions("ORBIT", rows)}


# --------------------------------------------------------------------------- #
def _iso(v) -> str | None:
    if v is None:
        return None
    try:
        return pd.Timestamp(v).isoformat()
    except (TypeError, ValueError):
        return None


def overview(runtime, orbit_dir: Path | None = None, now=None, commit: dict | None = None) -> dict:
    now = _ny(now if now is not None else pd.Timestamp.now(tz=NY))
    bots = [ghost_card(runtime, now), orbit_card(orbit_dir, now)]
    totals = {b["name"]: b["total"]["net_usd"] for b in bots}
    strategies = []
    for st in STRATEGIES:
        live = next((b for b in bots if st["status"] == "en vivo" and b["name"] == st["bot"]), None)
        strategies.append({**st, "live": ({"n": live["total"]["n"], "net_usd": live["total"]["net_usd"]}
                                          if live else None)})
    return {"now": now.isoformat(), "commit": commit or {}, "bots": bots,
            "strategies": strategies, "combined_usd": round(sum(totals.values()), 2)}
