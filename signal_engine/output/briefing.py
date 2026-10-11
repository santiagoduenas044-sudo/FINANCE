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
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

NY = "America/New_York"
STATE = Path(__file__).resolve().parents[2] / "data_store" / "briefing.json"
ROUND = 100.0            # round numbers every 100 pts
NEAR = 300.0             # levels within this many points are listed
MERGE = 12.0             # levels closer than this are one level
MIN_TOUCHES = 3          # a level the price turned at this many times counts
_ORDER = ("máximo de ayer", "mínimo de ayer", "cierre de ayer", "máximo de la noche",
          "mínimo de la noche", "número redondo", "nivel que frenó al precio")
log = logging.getLogger(__name__)


@dataclass
class Level:
    price: float
    names: list[str]
    touches: int = 0

    @property
    def label(self) -> str:
        names = sorted(self.names, key=lambda n: _ORDER.index(n) if n in _ORDER else 99)
        if len(names) > 1 and "nivel que frenó al precio" in names:
            names.remove("nivel que frenó al precio")       # said by "frenó N veces"
        text = " + ".join(names)
        if self.touches >= MIN_TOUCHES:
            text = (text + " · " if names != ["nivel que frenó al precio"] else "") + \
                f"frenó al precio {self.touches} veces"
        return text


def _ny(idx):
    idx = pd.DatetimeIndex(idx)
    return idx.tz_localize(NY) if idx.tz is None else idx.tz_convert(NY)


def _bars(m1: pd.DataFrame) -> pd.DataFrame:
    b = m1[["open", "high", "low", "close"]].copy()
    b.index = _ny(b.index)
    return b.sort_index()


def _session_start(now: pd.Timestamp) -> pd.Timestamp:
    """18:00 ET of the evening the current trading day began."""
    s = now.normalize() + pd.Timedelta(hours=18)
    return s if now >= s else s - pd.Timedelta(days=1)


def day_parts(m1: pd.DataFrame, now) -> dict:
    """Yesterday's day session (9:30–16:00) and tonight's session so far."""
    b = _bars(m1)
    now = pd.Timestamp(now).tz_convert(NY) if pd.Timestamp(now).tzinfo else pd.Timestamp(now).tz_localize(NY)
    b = b[b.index <= now]
    start = _session_start(now)
    night = b[(b.index > start) & (b.index <= now)]
    hm = b.index.strftime("%H:%M")
    rth = b[(hm > "09:30") & (hm <= "16:00") & (b.index <= start)]
    prev = None
    if not rth.empty:
        last_day = rth.index[-1].date()
        prev = rth[rth.index.date == last_day]
    return {"bars": b, "night": night, "prev": prev, "now": now,
            "price": float(b["close"].iloc[-1]) if not b.empty else None}


def turning_points(b: pd.DataFrame, tol: float = 12.0, side: int = 2) -> list[Level]:
    """Prices where the 15-minute candles turned two or more times."""
    m15 = b.resample("15min", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    pts = []
    for i in range(side, len(m15) - side):
        hi, lo = m15["high"].iloc[i], m15["low"].iloc[i]
        win = m15.iloc[i - side:i + side + 1]
        if hi >= win["high"].max():
            pts.append(float(hi))
        if lo <= win["low"].min():
            pts.append(float(lo))
    pts.sort()
    out: list[Level] = []
    group: list[float] = []
    for p in pts:
        if group and p - group[0] > tol:
            if len(group) >= MIN_TOUCHES:
                out.append(Level(sum(group) / len(group), ["nivel que frenó al precio"], len(group)))
            group = []
        group.append(p)
    if len(group) >= MIN_TOUCHES:
        out.append(Level(sum(group) / len(group), ["nivel que frenó al precio"], len(group)))
    return out


def key_levels(parts: dict) -> list[Level]:
    price = parts["price"]
    lv: list[Level] = []
    prev, night = parts["prev"], parts["night"]
    if prev is not None and not prev.empty:
        lv += [Level(float(prev["high"].max()), ["máximo de ayer"]),
               Level(float(prev["low"].min()), ["mínimo de ayer"]),
               Level(float(prev["close"].iloc[-1]), ["cierre de ayer"])]
    if night is not None and not night.empty:
        lv += [Level(float(night["high"].max()), ["máximo de la noche"]),
               Level(float(night["low"].min()), ["mínimo de la noche"])]
    lv += turning_points(parts["bars"][parts["bars"].index >= parts["now"] - pd.Timedelta(days=3)])
    if price is not None:
        base = round(price / ROUND) * ROUND
        lv += [Level(base + k * ROUND, ["número redondo"]) for k in range(-3, 4)]
    lv = [x for x in lv if price is not None and abs(x.price - price) <= NEAR]
    # one level per price area
    merged: list[Level] = []
    for x in sorted(lv, key=lambda x: x.price):
        if merged and x.price - merged[-1].price <= MERGE:
            m = merged[-1]
            if m.names == ["número redondo"] and "número redondo" not in x.names:
                m.price = x.price                      # the real level, not the round one
            m.names += [n for n in x.names if n not in m.names]
            m.touches = max(m.touches, x.touches)
        else:
            merged.append(Level(x.price, list(x.names), x.touches))
    return merged


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
