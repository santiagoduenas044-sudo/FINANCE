"""Key price levels: the supports and resistances the price is likely to
react at — yesterday's high / low / close, the night's high / low, levels the
15-minute candles turned at three or more times, and round numbers.

Used by the morning plan (output/briefing.py) and by GHOST's "espacio libre"
rule: do not take a trade when one of these levels sits in the first part of
the way to the target (a long right under a resistance, a short right on top
of a support).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

NY = "America/New_York"
ROUND = 100.0            # round numbers every 100 pts
NEAR = 300.0             # levels within this many points are listed
MERGE = 12.0             # levels closer than this are one level
MIN_TOUCHES = 3          # a level the price turned at this many times counts
_ORDER = ("máximo de ayer", "mínimo de ayer", "cierre de ayer", "máximo de la noche",
          "mínimo de la noche", "número redondo", "nivel que frenó al precio")


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


def levels_at(bars: pd.DataFrame, now) -> list[Level]:
    """The key levels as of `now` from 1m bars (only bars up to `now`)."""
    if bars is None or bars.empty:
        return []
    return key_levels(day_parts(bars, now))


def in_the_way(direction: str, entry: float, target: float, levels: list[Level],
               room: float, gap: float = 2.0) -> Level | None:
    """The first real level between the entry and `room` of the way to the
    target, or None if the path is clear. Round numbers alone do not count
    (there is one every 100 pts); levels within `gap` pts of the entry are the
    entry itself."""
    reach = room * abs(target - entry)
    hits = []
    for x in levels:
        if x.names == ["número redondo"]:
            continue
        d = (x.price - entry) if direction == "LONG" else (entry - x.price)
        if gap < d <= reach:
            hits.append((d, x))
    return min(hits, key=lambda h: h[0])[1] if hits else None


def efficiency(bars: pd.DataFrame, now, minutes: int = 120, step: str = "5min") -> float | None:
    """How straight the price went over the last `minutes`: net move of the
    5-minute closes / the path they walked (0 = back and forth, 1 = a line).
    None without enough bars."""
    if bars is None or bars.empty:
        return None
    b = _bars(bars)
    now = pd.Timestamp(now)
    now = now.tz_convert(NY) if now.tzinfo else now.tz_localize(NY)
    b = b[(b.index > now - pd.Timedelta(minutes=minutes)) & (b.index <= now)]
    c = b["close"].resample(step, label="right", closed="right").last().dropna()
    if len(c) < 6:
        return None
    path = float(c.diff().abs().sum())
    return abs(float(c.iloc[-1] - c.iloc[0])) / path if path > 0 else 0.0
