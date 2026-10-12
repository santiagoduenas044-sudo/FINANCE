"""The morning plan on Discord (9:00 ET, Monday to Friday).

Before New York opens, one message in the general channel with:
  * the key levels above (resistances) and below (supports): yesterday's
    high / low / close, the night's high / low, levels the price turned at
    several times, and round numbers (every 100 pts);
  * a reading of the day: does it lean to buys (LONG), sells (SHORT) or
    neither, and why, from yesterday's close and what the night did;
  * today's US news (CPI, jobs, Fed…) with their time.
With a chart of the last two days and the levels drawn.

It is a reading of where the price is, not a prediction, and it does not
change what the bots do. BOT_PLAN=off turns it off; BOT_PLAN_HORA moves it.
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

import pandas as pd

from ..ghost.levels import (MIN_TOUCHES, NEAR, NY, ROUND, Level,  # noqa: F401
                            day_parts, key_levels, turning_points)

log = logging.getLogger(__name__)
STATE = Path(__file__).resolve().parents[2] / "data_store" / "briefing.json"


def bias(parts: dict) -> tuple[str, list[str]]:
    """("LONG" / "SHORT" / "NEUTRAL", reasons). A reading, not a forecast."""
    price, prev, night = parts["price"], parts["prev"], parts["night"]
    score, why = 0, []
    if prev is not None and not prev.empty:
        move = float(prev["close"].iloc[-1] - prev["open"].iloc[0])
        score += 1 if move > 0 else -1
        why.append(f"ayer cerró {'al alza' if move > 0 else 'a la baja'} ({move:+.0f} pts)")
        gap = price - float(prev["close"].iloc[-1])
        score += 1 if gap > 0 else -1
        why.append(f"de noche {'subió' if gap > 0 else 'bajó'} {abs(gap):.0f} pts desde el cierre de ayer")
    if night is not None and not night.empty:
        hi, lo = float(night["high"].max()), float(night["low"].min())
        if hi > lo:
            where = (price - lo) / (hi - lo)
            if where >= 0.66:
                score += 1
                why.append("está en la parte alta del rango de la noche")
            elif where <= 0.33:
                score -= 1
                why.append("está en la parte baja del rango de la noche")
            else:
                why.append("está a la mitad del rango de la noche")
    side = "LONG" if score >= 2 else "SHORT" if score <= -2 else "NEUTRAL"
    return side, why


def build(m1: pd.DataFrame, now, news=None) -> tuple[str, str, int, bytes | None]:
    """(title, text, color, chart png)."""
    parts = day_parts(m1, now)
    price, now = parts["price"], parts["now"]
    levels = key_levels(parts)
    res = [x for x in levels if x.price > price + 1][:3]
    sup = [x for x in reversed(levels) if x.price < price - 1][:3]
    side, why = bias(parts)
    lines = [f"NQ ahora **{price:,.2f}**"]
    if side == "LONG":
        lines.append("**El día se inclina a COMPRAS (LONG)**" + (
            f" · mejor si respeta {sup[0].price:,.0f}" if sup else ""))
    elif side == "SHORT":
        lines.append("**El día se inclina a VENTAS (SHORT)**" + (
            f" · mejor si no pasa {res[0].price:,.0f}" if res else ""))
    else:
        lines.append("**Sin lado claro: esperar** que rompa "
                     + (f"{res[0].price:,.0f}" if res else "arriba")
                     + " o " + (f"{sup[0].price:,.0f}" if sup else "abajo"))
    lines += [f"• {w}" for w in why]
    lines.append("")
    lines.append("🔴 **Resistencias** (arriba)")
    lines += [f"{x.price:,.2f} · {x.label} · +{x.price - price:.0f} pts" for x in reversed(res)] \
        or ["ninguna cerca"]
    lines.append("🟢 **Soportes** (abajo)")
    lines += [f"{x.price:,.2f} · {x.label} · −{price - x.price:.0f} pts" for x in sup] \
        or ["ninguno cerca"]
    lines.append("")
    lines.append("📰 **Noticias de hoy (EE.UU.)**")
    evs = []
    if news is not None:
        try:
            evs = news.on_day(now, impacts=("High", "Medium"))
        except Exception:  # noqa: BLE001
            evs = []
    lines += [f"{e.when:%H:%M} · {e.title}" + (" · **fuerte**" if e.impact == "High" else "")
              for e in evs] or ["sin noticias importantes"]
    lines.append("")
    lines.append("_Lectura del precio, no una predicción. Los bots siguen sus reglas._")
    color = 0x2ECC71 if side == "LONG" else 0xE74C3C if side == "SHORT" else 0x95A5A6
    days = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    title = f"🧭 Plan del día · {days[now.weekday()]} {now:%d-%m}"
    return title, "\n".join(lines), color, chart(parts, res + sup)


def chart(parts: dict, levels: list[Level]) -> bytes | None:
    """The last two days on 15-minute candles with the levels drawn."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        b = parts["bars"][parts["bars"].index >= parts["now"] - pd.Timedelta(hours=40)]
        m15 = b.resample("15min", label="right", closed="right").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        if m15.empty:
            return None
        fig = Figure(figsize=(9, 4.6), facecolor="#0f1115")
        ax = fig.add_axes([0.06, 0.08, 0.80, 0.86], facecolor="#0f1115")
        x = range(len(m15))
        up = m15["close"] >= m15["open"]
        for i, (o, h, l, c, u) in enumerate(zip(m15["open"], m15["high"], m15["low"],
                                                m15["close"], up)):
            col = "#26a69a" if u else "#ef5350"
            ax.vlines(i, l, h, color=col, linewidth=0.8)
            ax.add_patch(matplotlib.patches.Rectangle((i - 0.3, min(o, c)), 0.6,
                                                      max(abs(c - o), 0.25), color=col))
        price = parts["price"]
        for lv in levels:
            col = "#ef5350" if lv.price > price else "#26a69a"
            ax.axhline(lv.price, color=col, linewidth=1, linestyle=(0, (4, 3)), alpha=.85)
            ax.text(len(m15) + 0.5, lv.price, f" {lv.price:,.0f}", color=col, fontsize=8,
                    va="center")
        ax.axhline(price, color="#e0e0e0", linewidth=0.8)
        ticks = list(range(0, len(m15), max(1, len(m15) // 8)))
        ax.set_xticks(ticks)
        ax.set_xticklabels([m15.index[i].strftime("%a %H:%M") for i in ticks], fontsize=7,
                           color="#9aa0aa")
        ax.tick_params(colors="#9aa0aa", labelsize=7)
        for sp in ax.spines.values():
            sp.set_color("#2a2f3a")
        ax.set_xlim(-1, len(m15) + 6)
        ax.set_title("NQ · 15 min · niveles del día", color="#e0e0e0", fontsize=10, loc="left")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - a chart is never worth the message
        log.warning("plan chart failed (%s)", type(exc).__name__)
        return None


# --------------------------------------------------------------------------- #
def due(now, state: Path = STATE) -> bool:
    """Weekdays, from BOT_PLAN_HORA (09:00 ET) until 9:30, once a day."""
    if os.environ.get("BOT_PLAN", "on").strip().lower() in ("off", "0", "no", "false"):
        return False
    t = pd.Timestamp(now)
    t = t.tz_convert(NY) if t.tzinfo else t.tz_localize(NY)
    hour = os.environ.get("BOT_PLAN_HORA", "09:00").strip() or "09:00"
    if t.weekday() >= 5 or not (hour <= t.strftime("%H:%M") < "09:30"):
        return False
    try:
        last = json.loads(state.read_text()).get("day")
    except (OSError, ValueError):
        last = None
    return last != t.date().isoformat()


def mark_sent(now, state: Path = STATE) -> None:
    t = pd.Timestamp(now)
    t = t.tz_convert(NY) if t.tzinfo else t.tz_localize(NY)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"day": t.date().isoformat()}))
