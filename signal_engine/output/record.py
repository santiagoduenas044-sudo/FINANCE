"""The trading record in plain dollars, for Discord (daily message and the
!record command): today, this week, everything since the record started,
and the latest trades one line each. Pure formatting over closed paper
trades — never changes anything.
"""
from __future__ import annotations

import pandas as pd

_NY = "America/New_York"
_EXIT = {"TP1": "objetivo", "TP2": "objetivo", "EOD": "cierre 16:55",
         "INVALIDATION": "invalidado", "CANCELLED": "cancelado", "EXPIRED": "expirado"}


def _ts(v) -> pd.Timestamp | None:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize(_NY) if t.tzinfo is None else t.tz_convert(_NY)


def trading_day(ts) -> pd.Timestamp:
    """CME trading day (the 18:00 ET open counts as the next date)."""
    return (_ts(ts) + pd.Timedelta(hours=6)).normalize().tz_localize(None)


def usd(pts: float, pv: float) -> str:
    return f"{'+' if pts >= 0 else '−'}${abs(pts * pv):,.0f}"


def exit_label(t) -> str:
    reason = t.exit_reason.value if t.exit_reason else ""
    if reason == "STOP":
        if t.pnl_points > 0.01:
            return "BE+" if t.pnl_points <= 5 else "stop en ganancia"
        if abs(t.pnl_points) <= 0.01:
            return "breakeven"
        return "stop"
    return _EXIT.get(reason, reason.lower() or "cerrado")


def _mark(pts: float) -> str:
    return "✅" if pts > 0.01 else ("➖" if pts >= -0.01 else "🛑")


def won_lost(pts: list[float], pv: float) -> str:
    """'ganado +$1,200 · perdido −$300 · neto +$900' in the position's dollars."""
    won = sum(p for p in pts if p > 0) * pv
    lost = sum(p for p in pts if p < 0) * pv
    net = won + lost
    return (f"ganado +${won:,.0f} · perdido −${abs(lost):,.0f} · "
            f"**neto {'+' if net >= 0 else '−'}${abs(net):,.0f}**")


def _line(label: str, trades: list, pv: float) -> str:
    if not trades:
        return f"**{label}:** sin trades"
    pts = [t.pnl_points for t in trades]
    w = sum(1 for p in pts if p > 0.01)
    flat = sum(1 for p in pts if abs(p) <= 0.01)
    return (f"**{label}:** {len(pts)} trades · {w} ✅ {flat} ➖ {len(pts) - w - flat} 🛑 · "
            f"{won_lost(pts, pv)} ({sum(pts):+.1f} pts)")


def build_record(closed: list, now, pv: float, position: str, last: int = 8) -> str:
    """Markdown for Discord. `closed`: closed PaperTrades; `pv`: $ per point
    of the position the operator trades (5 MNQ = 10)."""
    trades = sorted((t for t in closed if t.closed_at is not None), key=lambda t: _ts(t.closed_at))
    day = trading_day(now)
    week0 = day - pd.Timedelta(days=day.dayofweek)
    today = [t for t in trades if trading_day(t.closed_at) == day]
    week = [t for t in trades if trading_day(t.closed_at) >= week0]
    lines = [f"📒 **Récord · {position}** (${pv:g} por punto, simulado)",
             _line(f"Hoy {day:%d-%b}", today, pv), _line("Esta semana", week, pv)]
    if trades:
        pts = [t.pnl_points for t in trades]
        eq = peak = dd = 0.0
        for p in pts:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        wins = sum(1 for p in pts if p > 0.01)
        first = trading_day(trades[0].closed_at)
        lines.append(f"**Total desde {first:%d-%b}:** {len(pts)} trades · gana {wins / len(pts):.0%} · "
                     f"{won_lost(pts, pv)} · peor caída {usd(-dd, pv)}")
        lines.append("")
        lines.append("**Últimos trades**")
        for t in trades[-last:][::-1]:
            num = (t.context or {}).get("idea_number")
            side = "COMPRA" if t.direction == "LONG" else "VENTA"
            when = _ts(t.closed_at)
            lines.append(f"{_mark(t.pnl_points)} {when:%d-%b %H:%M} · "
                         f"{'#' + str(num) + ' ' if num is not None else ''}{side} "
                         f"{t.entry:,.2f} → {(t.exit_price or 0):,.2f} · {exit_label(t)} · "
                         f"**{usd(t.pnl_points, pv)}**")
    else:
        lines.append("_todavía no hay trades cerrados_")
    return "\n".join(lines)
