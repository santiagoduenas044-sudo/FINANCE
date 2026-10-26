"""The trade messages on Discord, "option C" (chosen 26-oct): each one tells
why, the plan step by step, or the trade's story, and ends with the set up's
record. Used by Set up 1 (output/discord.py) and by every parallel set up
(runtime/shadows.alert_note), so all of them read the same.

Every function takes a PaperTrade.to_dict() (or the heads-up event) and
returns plain text; the chart goes as the message's image.
"""
from __future__ import annotations

import pandas as pd

NY = "America/New_York"
_TF = {"1m": "1 min", "5m": "5 min", "15m": "15 min", "1H": "1 hora", "1h": "1 hora"}


def _px(v) -> str:
    return "—" if v is None else f"{float(v):,.2f}".rstrip("0").rstrip(".")


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.0f}"


def _hm(iso) -> str:
    if not iso:
        return "—"
    t = pd.Timestamp(iso)
    t = t.tz_localize(NY) if t.tzinfo is None else t.tz_convert(NY)
    return t.strftime("%H:%M")


def _contracts(position: str) -> int:
    head = (position or "").split()
    return int(head[0]) if head and head[0].isdigit() else 0


def record_line(label: str, pts: list[float], per_pt: float, fee: float = 0.0) -> str:
    """'Set up 5: 9 trades · 67 % · +$3,611' (dollars after commissions)."""
    if not pts:
        return f"{label}: todavía sin trades cerrados"
    won = sum(1 for p in pts if p > 0.01)
    net = sum(pts) * per_pt - fee * len(pts)
    n = len(pts)
    return f"{label}: {n} {'trade' if n == 1 else 'trades'} · {round(100 * won / n)} % · {_usd(net)}"


def _rules(t: dict) -> dict:
    return (t.get("context") or {}).get("plan_rules") or {}


def _lock(t: dict, after_partial: bool = True):
    """Where the stop goes once it protects: entry +/- the offset (or None)."""
    r, entry = _rules(t), t.get("entry")
    if entry is None or (after_partial and not r.get("lock_after_partial")):
        return None
    off = float(r.get("be_offset") or 0.0)
    return entry + off if t.get("direction") == "LONG" else entry - off


def entry_text(t: dict, per_pt: float, position: str, record: str | None = None) -> str:
    """Why it entered and what it will do, step by step."""
    ctx = t.get("context") or {}
    long = t.get("direction") == "LONG"
    entry, stop = t.get("entry"), t.get("stop")
    tps = list(t.get("take_profits") or [x for x in (t.get("tp1"), t.get("tp2")) if x is not None])
    final = tps[-1] if tps else None
    rules = _rules(t)
    tf = _TF.get(t.get("timeframe") or "", t.get("timeframe") or "")
    lines = []
    if ctx.get("zone_bottom") is not None:
        de = f" de {tf}" if tf else ""
        lines += ["**Por qué**",
                  (f"• Primer regreso a la zona FVG{de} "
                   f"({_px(ctx['zone_bottom'])} – {_px(ctx['zone_top'])})"),
                  (f"• Una vela{de} cerró {'arriba' if long else 'abajo'} "
                   f"de la mitad ({_px(ctx.get('zone_mid'))})")]
        if ctx.get("target_level"):
            lines.append(f"• Objetivo antes de {ctx['target_level']}")
        lines.append("")
    steps = []
    if entry is not None and stop is not None:
        risk = abs(entry - stop)
        steps.append(f"Stop {_px(stop)} ({_usd(-risk * per_pt)})" if per_pt else f"Stop {_px(stop)}")
    n = _contracts(position)
    if len(tps) > 1 and rules.get("partial"):
        k = round(rules["partial"] * n) if n else 0
        got = abs(tps[0] - entry) * per_pt * rules["partial"]
        lock = _lock(t)
        part = f"cierra {k} MNQ" if k else f"cierra el {rules['partial']:.0%}"
        steps.append(f"En {_px(tps[0])}: {part} ({_usd(got)})"
                     + (f" y el stop sube a {_px(lock)}" if lock is not None else ""))
    elif rules.get("be_at") and final is not None:
        at = entry + rules["be_at"] * (final - entry)
        steps.append(f"Al {rules['be_at']:.0%} del camino ({_px(at)}): el stop sube a "
                     f"{_px(_lock(t, after_partial=False))}")
    if rules.get("time_exit"):
        steps.append(f"Si a los {rules['time_exit']} min no avanzó un tercio del camino, cierra")
    if final is not None:
        eod = rules.get("eod")
        steps.append(f"Objetivo {_px(final)}" + (f" ({_usd(abs(final - entry) * per_pt)})" if per_pt else "")
                     + (f" o cierre a las {eod}" if eod else ""))
    if steps:
        lines.append("**El plan**")
        lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    if record:
        lines += ["", f"_{record}_"]
    return "\n".join(lines)


def entry_title(t: dict, label: str) -> str:
    long = t.get("direction") == "LONG"
    return (f"{'🟢' if long else '🔴'} {label} — {'COMPRA' if long else 'VENTA'} "
            f"{t.get('instrument', '')} en {_px(t.get('entry'))}")


def _verdict(usd: float) -> str:
    if abs(usd) < 0.5:
        return "➖", "QUEDÓ EN $0"
    return ("✅", f"GANÓ {_usd(usd)}") if usd > 0 else ("🛑", f"PERDIÓ {_usd(usd)}")


def exit_title(t: dict, label: str, per_pt: float) -> tuple[str, int]:
    """('✅ Set up 5 — GANÓ +$840', color)."""
    pts = t.get("pnl_points") or 0.0
    if not per_pt:                                     # no dollar value known: points
        mark = "✅" if pts > 0.01 else ("➖" if pts >= -0.01 else "🛑")
        word = ("GANÓ" if pts > 0.01 else "PERDIÓ" if pts < -0.01 else "QUEDÓ EN")
        color = 0x2ECC71 if pts > 0.01 else (0x95A5A6 if pts >= -0.01 else 0xE74C3C)
        return f"{mark} {label} — {word} {pts:+.2f} pts", color
    usd = pts * per_pt
    mark, word = _verdict(usd)
    color = 0x2ECC71 if usd > 0.5 else (0x95A5A6 if usd >= -0.5 else 0xE74C3C)
    return f"{mark} {label} — {word}", color


def exit_text(t: dict, per_pt: float, position: str, record: str | None = None) -> str:
    """The trade's story, hour by hour."""
    ctx = t.get("context") or {}
    long = t.get("direction") == "LONG"
    entry, exitp = t.get("entry"), t.get("exit_price")
    sign = 1 if long else -1
    n = _contracts(position)
    pf = float(t.get("partial_fraction") or 0.0)
    k = round(pf * n) if (n and pf) else 0
    lines = [f"`{_hm(t.get('opened_at'))}` {'Compra' if long else 'Venta'} en {_px(entry)}"]
    tps = list(t.get("take_profits") or [])
    if t.get("tp1_hit") and pf and len(tps) > 1:
        got = abs(tps[0] - entry) * per_pt * pf
        lock = ctx.get("tp1_stop") if ctx.get("tp1_stop") is not None else _lock(t)
        lines.append(f"`{_hm(ctx.get('tp1_at'))}` 💰 Mitad {_px(tps[0])}: cierra "
                     + (f"{k} MNQ" if k else f"el {pf:.0%}") + f" {_usd(got)}"
                     + (f" · stop a {_px(lock)}" if lock is not None else ""))
    rest = 1 - pf if (t.get("tp1_hit") and pf and len(tps) > 1) else 1.0
    leg_pts = (exitp - entry) * sign if exitp is not None and entry is not None else 0.0
    money = _usd(leg_pts * per_pt * rest) if per_pt else f"{leg_pts:+.2f} pts"
    left = (n - k) if (n and rest < 1) else None
    who = f" {left} MNQ" if left else ""
    then = (": cierra" + who) if who else ""
    why = (t.get("exit_reason") or "").upper()
    if why in ("TP2", "TP1"):
        what = f"🎯 Objetivo {_px(exitp)}: cierra{who} {money}"
    elif why == "STOP" and leg_pts > 0.01:
        what = f"🛡️ Stop protegido {_px(exitp)}: cierra{who} {money}"
    elif why == "STOP":
        what = f"🛑 Stop {_px(exitp)}{then} {money}"
    elif why == "EXPIRED":
        what = f"⏱ No avanzó: cierre por tiempo en {_px(exitp)}{then} {money}"
    elif why == "EOD":
        what = f"🕔 Cierre del día en {_px(exitp)}{then} {money}"
    else:
        what = f"Cerró en {_px(exitp)} {money}"
    lines.append(f"`{_hm(t.get('closed_at'))}` {what}")
    mae, mfe = t.get("mae_points"), t.get("mfe_points")
    if mae is not None and mfe is not None:
        def sp(v):
            return f"{'+' if v >= 0 else '−'}{abs(v):.0f}"
        lines += ["", f"Lo más que fue en contra: {sp(mae)} pts · lo más a favor: {sp(mfe)} pts"]
    if ctx.get("close_note"):
        lines.append(f"_{ctx['close_note']}_")
    if record:
        lines += ["", f"_{record}_"]
    return "\n".join(lines)


def alert_title(ev: dict, label: str) -> str:
    plan = ev.get("plan") or {}
    side = "COMPRA" if ev.get("direction") == "LONG" else "VENTA"
    dist = plan.get("distance")
    if dist is None and plan.get("price_now") is not None and ev.get("mid") is not None:
        dist = abs(plan["price_now"] - ev["mid"])
    away = f" · a {dist:.0f} pts" if dist is not None else ""
    return f"🟡 {label} — Zona de {side} en {ev.get('symbol', '')}{away}"


def alert_text(ev: dict, per_pt: float, record: str | None = None) -> str:
    plan = ev.get("plan") or {}
    long = ev.get("direction") == "LONG"
    mid, stop, final = ev.get("mid"), ev.get("stop"), plan.get("final")
    risk = abs(mid - stop) if mid is not None and stop is not None else None
    parts = [(f"Si una vela rebota en la zona ({_px(ev.get('bottom'))} – {_px(ev.get('top'))}), "
              f"{'compro' if long else 'vendo'} ~{_px(mid)}")]
    if stop is not None:
        parts.append(f"stop {_px(stop)}" + (f" ({_usd(-risk * per_pt)})" if per_pt and risk else ""))
    if final is not None and mid is not None:
        parts.append(f"objetivo {_px(final)}" + (f" ({_usd(abs(final - mid) * per_pt)})" if per_pt else ""))
    lines = [" · ".join(parts) + ".", "**No entres aún.**"]
    if record:
        lines += ["", f"_Idea #{ev.get('number', '?')} · {record}_"]
    else:
        lines += ["", f"_Idea #{ev.get('number', '?')}_"]
    return "\n".join(lines)
