"""The dashboard's bot hub: every bot's state, what it is looking at right now,
its record (today, total and per version) and the strategy catalogue.

GHOST runs inside this process, so it is read from the runtime. ORBIT is a
separate process: it is read from the files it saves every poll (its state
file doubles as its heartbeat).
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

import pandas as pd

from ..output.record import commission_per_trade
from ..versions import (BOT_VERSIONS, MAIN_SETUP, STRATEGIES, current_version,
                        ghost_version_of, main_setup_label, orbit_version_of, version_name,
                        version_span, version_title)

NY = "America/New_York"
log = logging.getLogger(__name__)
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
    "news_event": "noticia fuerte", "against_trend": "contra la tendencia (VWAP)",
    "level_in_the_way": "soporte/resistencia en el camino",
    "late_entry": "llegó tarde", "choppy_market": "serrucho", "against_plan": "contra el plan del día",
}


def _px(v) -> str:
    return "—" if v is None or not math.isfinite(float(v)) else f"{float(v):,.2f}"


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


def _versions(bot: str, rows: list[tuple[str, float]], variants: list[dict] = ()) -> list[dict]:
    """rows: (version, $) per closed trade -> one line per version, newest first.
    A version that has a parallel variant carries its id and switch."""
    by_version = {v["version"]: v for v in variants if v.get("version")}
    out = []
    for vid, start, title, rules in BOT_VERSIONS[bot]:
        line = {"version": vid, "since": start, "title": title, "rules": rules,
                "name": version_name(bot, vid), "span": version_span(bot, vid),
                **_record([u for v, u in rows if v == vid])}
        if vid in by_version:
            line["variant"] = by_version[vid]["id"]
            line["variant_on"] = by_version[vid]["enabled"]
        out.append(line)
    out[-1]["current"] = True
    return out[::-1]


def _main_record(bot: str, rows: list[tuple[str, float, bool]]) -> dict:
    """Today and total of the version running now only: (version, $, is today)."""
    cur = current_version(bot)
    title = next(t for v, _s, t, _r in BOT_VERSIONS[bot] if v == cur)
    mine = [(u, today) for v, u, today in rows if v == cur]
    return {"version": cur, "title": title, "today": _record([u for u, d in mine if d]),
            "total": _record([u for u, _ in mine])}


# --------------------------------------------------------------------------- #
# GHOST
# --------------------------------------------------------------------------- #
def _since(folder: Path) -> str | None:
    """'30 sep': when a set up's folder was created (when it started running)."""
    try:
        st = Path(folder).stat()
    except OSError:
        return None
    t = pd.Timestamp(getattr(st, "st_birthtime", None) or st.st_ctime, unit="s", tz="UTC").tz_convert(NY)
    return f"{t.day} {('ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic')[t.month - 1]}"


def _last_signal(rt, sym: str) -> str | None:
    """What a GHOST set up last decided: taken, or discarded and why."""
    last = (getattr(rt, "ghost_last_decision", {}) or {}).get(sym)
    if not last or not last.get("timestamp"):
        return None
    when = _ny(last["timestamp"]).strftime("%d-%b %H:%M")
    if last.get("action") in ("LONG", "SHORT"):
        return f"Última señal ({when}): {'compra' if last['action'] == 'LONG' else 'venta'} tomada."
    why = " · ".join(_REASONS_ES.get(r, r.replace("_", " "))
                     for r in last.get("no_trade_reasons") or []) or "sin motivo"
    return f"Última señal ({when}): descartada — {why}."


def ghost_variants(runtime, sym: str, pv: float, today, shadows_dir: Path | None) -> list[dict]:
    """Every GHOST variant in config/shadows.yaml, on or off. A running one is
    read from its live runtime; a switched-off one from its saved record."""
    from ..paper.tracker import PaperTradeTracker
    from ..runtime.shadows import SHADOWS_DIR, is_on, load_specs, store_dir
    base = shadows_dir or SHADOWS_DIR
    running = {sp.id: rt for sp, rt in (getattr(runtime, "shadows", None) or [])}
    out = []
    for spec in load_specs("ghost"):
        rt = running.get(spec.id)
        if rt is not None:
            tr = rt.paper_tracker
        else:
            d = store_dir("ghost", spec.id, base) / "paper"
            tr = PaperTradeTracker(store_dir=d) if d.exists() else None
        s_open = tr.active(sym) if tr is not None else []
        s_closed = [t for t in (tr.closed_trades if tr is not None else [])
                    if t.closed_at is not None and t.instrument == sym]
        on = is_on("ghost", spec, base)
        now_txt = "Apagado." if not on else ("Encendiéndose…" if rt is None else
                   _last_signal(rt, sym) or "Sin señal todavía: ninguna vela ha cumplido sus reglas "
                                            "desde que arrancó.")
        if s_open and rt is not None:
            t = s_open[0]
            now_txt = (f"{'Compra' if t.direction == 'LONG' else 'Venta'} abierta en {_px(t.entry)}"
                       f" · va {t.pnl_points:+.2f} pts ({_usd(t.pnl_points * pv)})")
        out.append({
            "id": spec.id, "name": spec.label, "code": spec.code, "setup": spec.setup,
            "about": spec.about, "version": spec.version,
            "since": _since(store_dir("ghost", spec.id, base)),
            "enabled": on, "retired": spec.retired, "running": rt is not None, "now": now_txt,
            "open_usd": round(sum(t.pnl_points for t in s_open) * pv, 2)
            if s_open and rt is not None else None,
            "today": _record([t.pnl_points * pv for t in s_closed
                              if _ny(t.closed_at).date() == today]),
            "total": _record([t.pnl_points * pv for t in s_closed])})
    return out


def ghost_card(runtime, now: pd.Timestamp, shadows_dir: Path | None = None) -> dict:
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
    rows = [(ghost_version_of(t), t.pnl_points * pv) for t in closed]
    variants = ghost_variants(runtime, sym, pv, today, shadows_dir)
    return {
        "variants": variants,
        "name": "GHOST", "color": "gold", "setup": MAIN_SETUP["GHOST"],
        "setup_label": main_setup_label("GHOST"),
        "strategy": version_title("GHOST", current_version("GHOST")).split(" · ")[0],
        "position": label,
        "status": status, "state": state, "symbol": sym,
        "heartbeat": _iso(getattr(runtime, "last_heartbeat_at", None)),
        "fee_per_trade": fee,
        "now": lines,
        "today": _record([t.pnl_points * pv for t in closed if _ny(t.closed_at).date() == today]),
        "total": _record([u for _, u in rows]),
        # the version running now, on its own (the "principal" row next to the variants)
        "main": _main_record("GHOST", [(v, u, _ny(t.closed_at).date() == today)
                                       for (v, u), t in zip(rows, closed)]),
        "open_usd": round(sum(t.pnl_points for t in open_t) * pv, 2) if open_t else None,
        "versions": _versions("GHOST", rows, variants),
    }


# --------------------------------------------------------------------------- #
# ORBIT
# --------------------------------------------------------------------------- #
def orbit_variants(shadows_dir: Path | None, now: pd.Timestamp) -> list[dict]:
    """ORBIT's parallel APERTURA variants, read from their folders."""
    from dataclasses import replace
    from ..runtime.shadows import SHADOWS_DIR, is_on, load_specs, store_dir
    from ..subbots.opening_range import OpeningRangeBot
    from ..subbots.orbit import settings
    today = now.date().isoformat()
    out = []
    for spec in load_specs("orbit"):
        try:
            rules = replace(settings(), **(spec.settings or {}))
        except (TypeError, ValueError):
            continue
        d = store_dir("orbit", spec.id, shadows_dir or SHADOWS_DIR)
        bot = OpeningRangeBot(d, settings=rules, name=spec.name)
        t = bot.st.trade
        now_txt, open_usd = "Sin trade abierto.", None
        if t is not None:
            px = bot.st.last_close
            pts = None if px is None else (px - t["entry"]) if t["long"] else (t["entry"] - px)
            if pts is not None:
                open_usd = round(pts * rules.usd_per_point * rules.contracts, 2)
            now_txt = (f"{'Compra' if t['long'] else 'Venta'} abierta en {_px(t['entry'])}"
                       + (f" · va {pts:+.2f} pts ({_usd(open_usd)})" if pts is not None else ""))
        elif bot.st.day == today and bot.st.done:
            now_txt = "Terminó por hoy."
        elif not (d / "apertura_state.json").exists():
            now_txt = "Todavía no ha corrido (arranca con ORBIT)."
        on = is_on("orbit", spec, shadows_dir or SHADOWS_DIR)
        if not on:
            now_txt = "Apagado."
        usd = [float(c.get("usd", 0)) for c in bot.closed]
        out.append({"id": spec.id, "name": spec.label, "code": spec.code, "setup": spec.setup,
                    "about": spec.about, "now": now_txt, "since": _since(d),
                    "version": spec.version, "enabled": on, "retired": spec.retired,
                    "open_usd": open_usd,
                    "today": _record([float(c.get("usd", 0)) for c in bot.closed
                                      if c.get("day") == today]),
                    "total": _record(usd)})
    return out


def orbit_card(store_dir: Path | None, now: pd.Timestamp, shadows_dir: Path | None = None) -> dict:
    from ..subbots.opening_range import OpeningRangeBot
    from ..subbots.orbit import STORE_DIR, bot_name, settings
    store_dir = Path(store_dir or STORE_DIR)
    s = settings()
    base = {"name": bot_name(), "color": "violet", "strategy": "APERTURA",
            "retired": _orbit_retired(),
            "setup": MAIN_SETUP["ORBIT"], "setup_label": main_setup_label("ORBIT"),
            "position": s.position, "fee_per_trade": commission_per_trade(s.position),
            "variants": orbit_variants(shadows_dir, now)}
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
            open_usd = round(pts * t.get("usd_per_point", s.usd_per_point)
                             * t.get("contracts", s.contracts), 2)
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
    rows = [(orbit_version_of(c),
             float(c.get("usd", 0))) for c in bot.closed]
    return {**base, "status": "ONLINE" if alive else "STALE", "state": state,
            "heartbeat": pd.Timestamp(state_file.stat().st_mtime, unit="s", tz="UTC").isoformat(),
            "now": lines,
            "today": _record([float(c.get("usd", 0)) for c in bot.closed if c.get("day") == today]),
            "total": _record([u for _, u in rows]), "open_usd": open_usd,
            "main": _main_record("ORBIT", [(v, u, c.get("day") == today)
                                           for (v, u), c in zip(rows, bot.closed)]),
            "versions": _versions("ORBIT", rows)}


# --------------------------------------------------------------------------- #
def _iso(v) -> str | None:
    if v is None:
        return None
    try:
        return pd.Timestamp(v).isoformat()
    except (TypeError, ValueError):
        return None


def _clean(v):
    """JSON-safe: NaN/inf (e.g. a missing Yahoo price) become None instead of
    making the whole answer fail."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _broken(name: str, color: str, exc: Exception) -> dict:
    """A bot card that could not be built: the others still show, and this one
    says what went wrong (the full traceback goes to the dashboard log)."""
    return {"name": name, "color": color, "strategy": "—", "position": "—",
            "status": "ERROR", "state": "off", "heartbeat": None, "fee_per_trade": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "now": [{"kind": "alert", "text": f"No pude leer este bot: {type(exc).__name__}: {exc}"}],
            "today": _record([]), "total": _record([]), "open_usd": None,
            "variants": [], "versions": _versions(name, []) if name in BOT_VERSIONS else []}


def overview(runtime, orbit_dir: Path | None = None, now=None, commit: dict | None = None,
             shadows_dir: Path | None = None, lab_dir: Path | None = None) -> dict:
    now = _ny(now if now is not None else pd.Timestamp.now(tz=NY))
    bots = []
    for name, color, build in (("GHOST", "gold", lambda: ghost_card(runtime, now, shadows_dir)),
                               ("ORBIT", "violet", lambda: orbit_card(orbit_dir, now, shadows_dir))):
        try:
            bots.append(build())
        except Exception as exc:  # noqa: BLE001 - one bad bot never hides the others
            log.exception("bot hub: %s card failed", name)
            bots.append(_broken(name, color, exc))
    # no "together" total: every set up is a different strategy, measured alone
    return _clean({"now": now.isoformat(), "commit": commit or {}, "bots": bots,
                   "setups": setup_rows(bots),
                   "strategies": strategy_cards(bots, lab_dir),
                   "news": news_today(getattr(runtime, "news", None), now)})


def _orbit_retired() -> bool:
    from ..runtime.shadows import main_retired
    return main_retired("orbit")


def setup_rows(bots: list[dict]) -> list[dict]:
    """The set ups' control table: every strategy that trades (the bots
    themselves and their parallel set ups) in number order, with what it is
    doing now, today and its total (the main ones: the version in use)."""
    rows = []
    for b in bots:
        # $ per trade still to take off: GHOST's records are before commissions,
        # ORBIT's already after them
        fee = (b.get("fee_per_trade") or 0.0) if b["name"] == "GHOST" else 0.0
        if b.get("setup") and not b.get("retired"):
            main = b.get("main") or {}
            now = (b.get("now") or [{}])[0].get("text", "")
            rows.append({"setup": b["setup"], "label": b.get("setup_label") or b["name"],
                         "bot": b["name"], "kind": "main", "on": b.get("status") == "ONLINE",
                         "fee": fee,
                         "now": now, "open_usd": b.get("open_usd"),
                         "today": main.get("today") or b.get("today"),
                         "total": main.get("total") or b.get("total")})
        for v in b.get("variants") or []:
            if v.get("retired"):
                continue
            rows.append({"setup": v.get("setup") or 99, "label": v["name"], "bot": b["name"],
                         "kind": "variant", "id": v["id"], "on": v["enabled"], "fee": fee,
                         "since": v.get("since"),
                         "now": v.get("now", ""), "open_usd": v.get("open_usd"),
                         "today": v.get("today"), "total": v.get("total")})
    return sorted(rows, key=lambda r: r["setup"])


def setups_text(rows: list[dict]) -> str:
    """!setups on Discord: one line per set up."""
    def money(rec):
        if not rec or not rec.get("n"):
            return "sin trades"
        return f"{rec['n']} {'trade' if rec['n'] == 1 else 'trades'} {_usd(rec['net_usd'])}"
    out = ["📋 **Set ups** (en papel · mismo mercado)"]
    for r in rows:
        dot = "🟢" if r["on"] else "⚪"
        since = f" · corre desde el {r['since']}" if r.get("since") else ""
        out.append(f"{dot} **{r['label']}** ({r['bot']}) · hoy {money(r['today'])} · "
                   f"total {money(r['total'])}{since}\n　{r['now'] or ('Apagado.' if not r['on'] else '')}")
    return "\n".join(out)


def strategy_cards(bots: list[dict], lab_dir: Path | None = None) -> list[dict]:
    """The catalogue with each strategy's OWN paper record: the bot versions
    that traded it for real plus its parallel variants, never another
    strategy's trades (FVG 15 counts GHOST v4 only; FVG 1 DÍA gets v2's)."""
    cards = {b["name"]: b for b in bots}
    variants = {v["id"]: (b["name"], v) for b in bots for v in b.get("variants", [])}
    lab = latest_lab_rows(lab_dir)
    out = []
    for st in STRATEGIES:
        parts = []
        for bot, vids in (st.get("real") or {}).items():
            per = {v["version"]: v for v in cards.get(bot, {}).get("versions", [])}
            for vid in vids:
                v = per.get(vid)
                if v is None or (not v["n"] and not v.get("current")):
                    continue
                parts.append({"kind": "real", "bot": bot, "version": vid,
                              "name": (f"Set up {MAIN_SETUP[bot]} · {bot} {vid} (ahora)"
                                       if v.get("current") else f"{bot} {vid} (antes)"),
                              "n": v["n"], "net_usd": v["net_usd"]})
        on = []
        for sid in st.get("shadows") or []:
            if sid not in variants:
                continue
            bot, v = variants[sid]
            on.append(v["enabled"])
            parts.append({"kind": "paper", "bot": bot, "variant": sid,
                          "name": f"Set up {v.get('setup') or '?'}{'' if v['enabled'] else ' (apagado)'}",
                          "enabled": v["enabled"],
                          "n": v["total"]["n"], "net_usd": v["total"]["net_usd"]})
        status = st["status"]
        if status == "en paralelo" and on and not any(on):
            status = "apagada"
        # each part (a bot version, a set up) on its own: never added together
        live = {"detail": parts} if parts else None
        setups = [MAIN_SETUP[bot] for bot, vids in (st.get("real") or {}).items()
                  if bot in MAIN_SETUP and current_version(bot) in vids]
        setups += [variants[sid][1].get("setup") for sid in st.get("shadows") or []
                   if sid in variants and variants[sid][1].get("setup")]
        card = {**st, "status": status, "live": live, "setups": setups}
        if st.get("lab_prefix"):
            card["lab_run"] = lab_summary(lab, st["lab_prefix"])
        out.append(card)
    return out


def latest_lab_rows(lab_dir: Path | None, keep: int = 20) -> list[dict]:
    """The newest lab runs saved with their numbers (data_store/lab/*.json),
    newest first."""
    if lab_dir is None:
        return []
    import json
    out = []
    for f in sorted(Path(lab_dir).glob("*.json"), reverse=True)[:keep]:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if data.get("rows"):
            out.append({"id": f.stem, "meta": data.get("meta", {}), "rows": data["rows"]})
    return out


def lab_summary(runs: list[dict], prefix: str) -> dict | None:
    """How the variants of one idea did in the newest lab run that tried it:
    how many passed the blind test and the best one (by its TEST result)."""
    for lab in runs:
        rows = [r for r in lab["rows"]
                if r["name"] == prefix or r["name"].startswith(prefix + " ·")]
        if not rows:
            continue
        best = max(rows, key=lambda r: (r["ok"], (r.get("test") or {}).get("conf", -99)))
        te, tr, al = best.get("test") or {}, best.get("train") or {}, best.get("all") or {}
        return {"run": lab["id"], "position": lab["meta"].get("label"),
                "variants": len(rows), "passed": sum(1 for r in rows if r["ok"]),
                "best": {"name": best["name"], "ok": best["ok"],
                         "n": al.get("n", 0), "net_usd": al.get("net"),
                         "train_avg": tr.get("avg"), "test_avg": te.get("avg"),
                         "dd": al.get("dd")}}
    return None


# --------------------------------------------------------------------------- #
# every closed trade, for the calendar and the history
# --------------------------------------------------------------------------- #
def _ghost_rows(tracker, sym: str, pv: float, fee: float, src: str, version=None) -> list[dict]:
    out = []
    for t in (tracker.closed_trades if tracker is not None else []):
        if t.closed_at is None or t.instrument != sym:
            continue
        usd = t.pnl_points * pv
        out.append({"src": src, "version": version or ghost_version_of(t),
                    "day": _ny(t.closed_at).date().isoformat(),
                    "opened_at": _iso(t.opened_at), "closed_at": _iso(t.closed_at),
                    "side": t.direction, "entry": t.entry, "exit": t.exit_price,
                    "pts": round(t.pnl_points, 2), "usd": round(usd, 2),
                    "net_usd": round(usd - fee, 2),
                    # per contract, after commission: what 1 MNQ would have made
                    "net_pts": round(t.pnl_points - (fee / pv if pv else 0.0), 2),
                    "why": getattr(t.exit_reason, "value", None) or "",
                    "idea": (t.context or {}).get("idea_number")})
    return out


def _orbit_rows(bot, src: str, version=None) -> list[dict]:
    out = []
    for c in bot.closed:
        if not c.get("closed_at"):
            continue
        usd = float(c.get("usd", 0))
        out.append({"src": src, "version": version or orbit_version_of(c),
                    "day": c.get("day") or _ny(c["closed_at"]).date().isoformat(),
                    "opened_at": _iso(c.get("opened_at")), "closed_at": _iso(c["closed_at"]),
                    "side": "LONG" if c.get("long") else "SHORT", "entry": c.get("entry"),
                    "exit": c.get("exit"), "pts": c.get("pts"), "net_pts": c.get("net_pts"),
                    # ORBIT's dollars already have commission and slippage out
                    "usd": round(usd, 2), "net_usd": round(usd, 2),
                    "why": c.get("why", ""), "size": f"{c.get('contracts', '')} {c.get('symbol', '')}".strip()})
    return out


def trade_log(runtime, orbit_dir: Path | None = None, shadows_dir: Path | None = None) -> dict:
    """Every closed paper trade of every bot, version and parallel variant,
    each in dollars for the size it traded. GHOST's `usd` is before
    commissions and `net_usd` after them; ORBIT's are already net (as in its
    Discord messages). `sources` says what each `src` is."""
    from dataclasses import replace
    from ..paper.tracker import PaperTradeTracker
    from ..runtime.shadows import SHADOWS_DIR, is_on, load_specs, store_dir
    from ..subbots.opening_range import OpeningRangeBot
    from ..subbots.orbit import STORE_DIR, bot_name, settings
    base = shadows_dir or SHADOWS_DIR
    sources, trades = [], []

    # GHOST and its variants (all priced at GHOST's position)
    feeds = list(getattr(runtime, "live_feeds", None) or {}) or ["NQ"]
    sym = "NQ" if "NQ" in feeds else feeds[0]
    try:
        pos = runtime._point_values(sym) or {}
    except Exception:  # noqa: BLE001
        pos = {}
    label, pv = next(iter(pos.items())) if pos else (f"1 punto {sym}", 1.0)
    fee = commission_per_trade(label)
    sources.append({"id": "ghost", "bot": "GHOST", "kind": "main", "name": "GHOST",
                    "setup": MAIN_SETUP["GHOST"], "setup_label": main_setup_label("GHOST"),
                    "position": label, "fee": fee,
                    "versions": [{"version": v, "title": t, "name": version_name("GHOST", v),
                                  "span": version_span("GHOST", v)} for v, _s, t, _r in BOT_VERSIONS["GHOST"]],
                    "current": current_version("GHOST")})
    trades += _ghost_rows(getattr(runtime, "paper_tracker", None), sym, pv, fee, "ghost")
    running = {sp.id: rt for sp, rt in (getattr(runtime, "shadows", None) or [])}
    for spec in load_specs("ghost"):
        rt = running.get(spec.id)
        if rt is not None:
            tr = rt.paper_tracker
        else:
            d = store_dir("ghost", spec.id, base) / "paper"
            tr = PaperTradeTracker(store_dir=d) if d.exists() else None
        src = f"ghost:{spec.id}"
        sources.append({"id": src, "bot": "GHOST", "kind": "variant", "name": spec.label,
                        "setup": spec.setup,
                        "about": spec.about, "enabled": is_on("ghost", spec, base),
                        "retired": spec.retired,
                        "position": label, "fee": fee})
        trades += _ghost_rows(tr, sym, pv, fee, src, version=spec.version or "paralelo")

    # ORBIT and its variants
    s = settings()
    odir = Path(orbit_dir or STORE_DIR)
    from ..runtime.shadows import main_retired
    sources.append({"id": "orbit", "bot": "ORBIT", "kind": "main", "name": bot_name(),
                    "retired": main_retired("orbit"),
                    "setup": MAIN_SETUP["ORBIT"], "setup_label": main_setup_label("ORBIT"),
                    "position": s.position, "fee": 0,
                    "versions": [{"version": v, "title": t, "name": version_name("ORBIT", v),
                                  "span": version_span("ORBIT", v)} for v, _s, t, _r in BOT_VERSIONS["ORBIT"]],
                    "current": current_version("ORBIT")})
    if (odir / "apertura_trades.jsonl").exists():
        trades += _orbit_rows(OpeningRangeBot(odir, settings=s), "orbit")
    for spec in load_specs("orbit"):
        src = f"orbit:{spec.id}"
        sources.append({"id": src, "bot": "ORBIT", "kind": "variant", "name": spec.label,
                        "setup": spec.setup,
                        "about": spec.about, "enabled": is_on("orbit", spec, base),
                        "retired": spec.retired,
                        "position": s.position, "fee": 0})
        d = store_dir("orbit", spec.id, base)
        if (d / "apertura_trades.jsonl").exists():
            try:
                rules = replace(s, **(spec.settings or {}))
            except (TypeError, ValueError):
                rules = s
            trades += _orbit_rows(OpeningRangeBot(d, settings=rules), src, version="paralelo")
    trades.sort(key=lambda t: t["closed_at"] or "")
    for so in sources:
        mine = [t for t in trades if t["src"] == so["id"]
                and (so["kind"] != "main" or t["version"] == so.get("current"))]
        so["fondeo"] = {**funded_check(mine), "position": so.get("position")}
    return _clean({"sources": sources, "trades": trades})


# --------------------------------------------------------------------------- #
# would this set up survive a funded account? (its own trades only)
# --------------------------------------------------------------------------- #
FUNDED_ACCOUNT = os.environ.get("BOT_FONDEO_CUENTA", "25K")
MNQ_PER_POINT = 2.0
MIN_TRADES = 20                     # fewer than this: too early to trust


def _funded_limit() -> float:
    try:
        return float(os.environ.get("BOT_FONDEO_LIMITE", "") or 1500)
    except ValueError:
        return 1500.0


def funded_check(trades: list[dict], limit: float | None = None) -> dict:
    """One set up's closed trades (in close order) against a funded account
    that is lost after `limit` dollars of drawdown (25K: $1,500).

    - worst trade, worst day and the biggest drop from a high, in the
      dollars of the size it traded (commissions out);
    - the same drop for 1 MNQ, and how many MNQ fit under the limit with it.
    Never mixes set ups: the caller passes one set up's trades."""
    limit = _funded_limit() if limit is None else limit
    out = {"account": FUNDED_ACCOUNT, "limit_usd": limit, "n": len(trades),
           "few": len(trades) < MIN_TRADES}
    if not trades:
        return {**out, "fits": None}

    def drop(values):
        eq = peak = dd = 0.0
        for v in values:
            eq += v
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        return dd

    usd = [t.get("net_usd") or 0.0 for t in trades]
    by_day: dict[str, float] = {}
    for t, u in zip(trades, usd):
        by_day[t.get("day") or ""] = by_day.get(t.get("day") or "", 0.0) + u
    one = [(t.get("net_pts") if t.get("net_pts") is not None else t.get("pts") or 0.0)
           * MNQ_PER_POINT for t in trades]
    dd, dd_one = drop(usd), drop(one)
    return {**out,
            "worst_trade_usd": round(min(min(usd), 0.0), 2),
            "worst_day_usd": round(min(min(by_day.values()), 0.0), 2),
            "max_dd_usd": round(dd, 2),
            "max_dd_1mnq_usd": round(dd_one, 2),
            "fits": dd < limit,
            "mnq": int(limit // dd_one) if dd_one > 0 else None}


def news_today(calendar, now) -> dict:
    """Today's high-impact US releases (and market holidays) for the hub."""
    if calendar is None:
        return {"available": False, "events": []}
    try:
        evs = calendar.on_day(now, impacts=("High", "Holiday"))
    except Exception:  # noqa: BLE001
        return {"available": False, "events": []}
    return {"available": bool(calendar.events()), "events": [e.to_dict() for e in evs]}
