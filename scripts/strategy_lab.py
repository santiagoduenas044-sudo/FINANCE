"""Strategy lab: try simple, DIFFERENT trading ideas on years of 1-minute NQ in
minutes (the full bot replay takes a night), with the same blind test as
edge_finder: every variant is judged on TRAIN (before --split) and must also
make money on TEST (from --split on) to get a ✔.

    .venv/bin/python scripts/strategy_lab.py                       # 2021-09 → today
    .venv/bin/python scripts/strategy_lab.py --position 1nq --sessions all
    .venv/bin/python scripts/strategy_lab.py --position riesgo      # ~$300 at risk per trade

Sizing: a fixed position (5 MNQ / 1 NQ: a wider stop loses more dollars) or
--position riesgo: as many MNQ as keep the stop at about --risk-usd (a
wider stop uses fewer micros; the target is still k x the stop, so a win
still pays k times what a loss costs).

The "big companies" filter needs their 1-minute bars (NVDA, MSFT, AAPL...):
    PYTHONPATH=. .venv/bin/python scripts/fetch_databento.py --stocks grandes --start 2021-09-27
Without them those rows are skipped and the report says so.

Rules shared by every strategy (so they compare fairly):
  * signals are read on CLOSED bars of their timeframe; the entry is the open
    of the next 1-minute bar (no looking ahead);
  * one position at a time per strategy; stop and target are simulated on
    1-minute bars (if both fall inside the same minute, the stop counts);
    a stop that the market gaps through fills at the worse open;
  * everything flat at 16:55 ET (Apex); new entries 09:30-15:30 ET
    (--sessions all: any time except 16:15-18:00);
  * real costs: Tradovate commission + 1 tick of slippage per side.

It only reads market_data/; it never touches the bot or data_store/.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TICK = 0.25
# $ per point and round-trip commission (Tradovate at Apex, per position);
# "riesgo" is priced per MNQ and sized per trade (see sized_usd)
POSITIONS = {"5mnq": ("5 MNQ", 10.0, 5 * 1.04), "1nq": ("1 NQ", 20.0, 3.10),
             "riesgo": ("riesgo fijo en MNQ", 2.0, 1.04)}
REF_PRICE = 15_000.0                 # NQ around 2021-2022: what the point limits were set for
MAX_MICROS = 15                     # never more than 1.5 NQ, however tight the stop
# the largest Nasdaq-100 weights: their 1-minute bars feed the breadth filter
MEGACAPS = ("NVDA", "MSFT", "AAPL", "AMZN", "META", "AVGO", "GOOGL", "TSLA")


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def load_1m(symbol: str, data: str, start: str, end: str) -> pd.DataFrame:
    csv = ROOT / "market_data" / symbol / f"{data}_1m.csv"
    pq = csv.with_suffix(".parquet")
    if not csv.exists():
        raise SystemExit(f"No encuentro {csv.relative_to(ROOT)}")
    if pq.exists() and pq.stat().st_mtime >= csv.stat().st_mtime:
        df = pd.read_parquet(pq, filters=[("datetime", ">=", pd.Timestamp(start)),
                                          ("datetime", "<", pd.Timestamp(end) + pd.Timedelta(days=1))])
    else:
        print("  (primera vez: leyendo el CSV grande, ~1 min)", file=sys.stderr)
        df = pd.read_csv(csv, index_col="datetime", parse_dates=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_parquet(pq, row_group_size=50_000)
        df = df.loc[start:end]
    return df[["open", "high", "low", "close", "volume"]].astype(float)


class Market:
    """1-minute arrays for exits + resampled bars for signals (naive ET)."""

    def __init__(self, m1: pd.DataFrame):
        self.m1 = m1
        self.t = m1.index.values
        self.o, self.h = m1["open"].to_numpy(), m1["high"].to_numpy()
        self.l, self.c = m1["low"].to_numpy(), m1["close"].to_numpy()
        # first 1m index at/after 16:55 of each bar's CME trading day
        day = (m1.index + pd.Timedelta(hours=6)).normalize()
        self.eod = np.searchsorted(self.t, (day + pd.Timedelta(hours=16, minutes=55)).values)
        self._bars: dict[str, pd.DataFrame] = {}
        # --escala precio: point limits are written for NQ at REF_PRICE and
        # grow with the price (50 pts at 15,000 = 100 pts at 30,000). Uses the
        # previous day's close, so it never peeks at today.
        self.scaled = False
        close = m1["close"].groupby(day).last()
        self._day_scale = (close.shift(1) / REF_PRICE).bfill()

    def scale_at(self, times) -> np.ndarray:
        """Factor for point limits at each of `times` (1.0 without --escala)."""
        times = pd.DatetimeIndex(times)
        if not self.scaled:
            return np.ones(len(times))
        day = (times + pd.Timedelta(hours=6)).normalize()
        return self._day_scale.reindex(day).ffill().bfill().fillna(1.0).to_numpy()

    def bars(self, tf: str) -> pd.DataFrame:
        if tf not in self._bars:
            if tf == "1min":
                b = self.m1.copy()
            else:
                b = self.m1.resample(tf, label="left", closed="left").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last",
                     "volume": "sum"}).dropna(subset=["open"])
            b["end"] = b.index + pd.Timedelta(tf)          # when the bar is known
            self._bars[tf] = b
        return self._bars[tf]

    def asof(self, tf: str, values: np.ndarray, when) -> float:
        """values[] of the last `tf` bar CLOSED at `when` (nan before the first)."""
        end = self.bars(tf)["end"].to_numpy()
        i = int(np.searchsorted(end, np.datetime64(when), side="right")) - 1
        return float(values[i]) if i >= 0 else float("nan")

    def atr5(self) -> np.ndarray:
        """Average 5-minute candle (high-low, last 14): the market's normal move."""
        if "_atr5" not in self.__dict__:
            b = self.bars("5min")
            self._atr5 = (b["high"] - b["low"]).rolling(14, min_periods=5).mean().to_numpy()
        return self._atr5


# --------------------------------------------------------------------------- #
# simulation
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    ready: np.datetime64          # when it is known (bar close)
    long: bool
    stop: float
    k: float | None = None        # target = k x risk from the entry...
    target: float | None = None   # ...or a fixed price
    until: str | None = None      # "HH:MM": flat at that time of the entry day (else 16:55)


def _minutes(ts) -> int:
    ts = pd.Timestamp(ts)
    return ts.hour * 60 + ts.minute


def entry_open(ts, sessions: str) -> bool:
    m = _minutes(ts)
    if sessions == "rth":
        return 9 * 60 + 30 <= m <= 15 * 60 + 30
    return not (16 * 60 + 15 <= m < 18 * 60)


def run(mk: Market, signals: list[Signal], cost_pts: float, sessions: str,
        be_at: float | None = None, be_plus: float = 2.0,
        max_risk: float | None = None, steps: list[tuple[float, float]] | None = None,
        partial: tuple[float, float] | None = None) -> list[tuple[str, float, float, int]]:
    """One position at a time. Returns [(entry date, net points, stop points,
    entry hour ET)].
    `max_risk`: skip entries whose stop is farther than that (sizing limit).
    Protecting a trade that goes well (each rule from the NEXT minute on):
      be_at=0.7: at 70% of the way to the target, stop to entry + be_plus.
      steps=[(0.5, 0.0), (0.7, 0.3)]: at 50% of the way, stop to entry +
        be_plus; at 70%, stop to entry + 30% of the way (+ be_plus)...
      partial=(0.7, 0.5): at 70% close half there; the rest runs with its
        stop at entry + be_plus."""
    if be_at is not None and steps is None:
        steps = [(be_at, 0.0)]
    out, busy = [], 0
    for s in sorted(signals, key=lambda x: x.ready):
        i0 = int(np.searchsorted(mk.t, s.ready))
        if i0 < busy or i0 >= len(mk.t) or not entry_open(mk.t[i0], sessions):
            continue
        end = int(mk.eod[i0])
        if s.until:
            cut = pd.Timestamp(mk.t[i0]).normalize() + pd.Timedelta(s.until + ":00")
            end = min(end, int(np.searchsorted(mk.t, cut.to_datetime64())))
        if end <= i0:
            continue
        entry = mk.o[i0]
        risk = (entry - s.stop) if s.long else (s.stop - entry)
        if risk <= TICK or (max_risk is not None and risk > max_risk):
            continue
        tgt = s.target if s.target is not None else (
            entry + s.k * risk if s.long else entry - s.k * risk)
        if (tgt - entry) * (1 if s.long else -1) <= TICK:
            continue
        j, px = _exit(mk, i0, end, s.long, s.stop, tgt, entry, steps, be_plus, partial)
        pts = (px - entry) if s.long else (entry - px)
        when = pd.Timestamp(mk.t[i0])
        out.append((str(when.date()), pts - cost_pts, float(risk), when.hour))
        busy = j + 1
    return out


def _first(mask: np.ndarray) -> int:
    if not len(mask):
        return -1
    k = int(np.argmax(mask))
    return k if mask[k] else -1


def _exit(mk, i0, end, long, stop, tgt, entry, steps=None, be_plus=2.0, partial=None):
    """(exit index, exit price). With `partial` the price is the blend of
    the part closed early and the rest."""
    h, l, o = mk.h[i0:end], mk.l[i0:end], mk.o[i0:end]
    n = len(h)
    sgn = 1 if long else -1
    dist = abs(tgt - entry)

    def scan(stop_px, start):
        hs = (l[start:] <= stop_px) if long else (h[start:] >= stop_px)
        ht = (h[start:] >= tgt) if long else (l[start:] <= tgt)
        ks, kt = _first(hs), _first(ht)
        if ks < 0 and kt < 0:
            return None
        if ks >= 0 and (kt < 0 or ks <= kt):
            k = start + ks
            fill = min(stop_px, o[k]) if long else max(stop_px, o[k])     # gap through stop
            return k, fill
        return start + kt, tgt

    def reached(frac, start):
        trig = entry + sgn * frac * dist
        k = _first((h[start:] >= trig) if long else (l[start:] <= trig))
        return -1 if k < 0 else start + k

    cur, start = stop, 0
    rules = [(f, lock, False) for f, lock in (steps or [])]
    if partial is not None:
        rules.append((partial[0], 0.0, True))
    for frac, lock, is_partial in sorted(rules):
        if start >= n:
            break
        first = scan(cur, start)
        hit = reached(frac, start)
        if hit < 0 or (first is not None and first[0] <= hit):
            break                                   # exits (or ends) before this step
        new = entry + sgn * (lock * dist + be_plus)
        cur = max(cur, new) if long else min(cur, new)
        start = hit + 1                             # the stop moves from the next minute
        if is_partial:
            share = partial[1]
            done = entry + sgn * frac * dist        # this part closed at the step price
            rest = scan(cur, start) if start < n else None
            j, px = (i0 + rest[0], rest[1]) if rest else (end - 1, mk.c[end - 1])
            return j, share * done + (1 - share) * px
    res = scan(cur, start) if start < n else None
    if res is None:
        return end - 1, mk.c[end - 1]                      # 16:55 flat
    return i0 + res[0], res[1]


# --------------------------------------------------------------------------- #
# strategies (each returns signals; the runner decides entries and exits)
# --------------------------------------------------------------------------- #
def zones(mk: Market, tf: str, k: float, stop: str = "origen", buffer: float = 3.0,
          atr_mult: float = 1.5, lookback_min: int = 60) -> list[Signal]:
    """The bot's idea on any timeframe: a fresh fair-value gap, the FIRST
    return into it, and a candle that closes back past the zone's middle in
    the gap's direction.

    stop="origen": behind the move's origin (1 pt), as the bot does.
    stop="dinamico": behind whichever is farther, the origin or the last
    hour's high/low (the resistance/support price just tested), plus
    `buffer` pts, and never closer than `atr_mult` average 5-minute candles
    (a busy market gets more room).
    stop="ghost": like the live bot, behind the origin but kept 20-35 pts
    from the entry.

    Every limit in points (gap size, stop size, buffer) is multiplied by the
    price factor with --escala precio (see Market.scale_at)."""
    b = mk.bars(tf)
    O, H, L, C = (b[c].to_numpy() for c in ("open", "high", "low", "close"))
    end = b["end"].to_numpy()
    f = mk.scale_at(b.index)                   # 1.0, or price / REF_PRICE
    mins = int(pd.Timedelta(tf).total_seconds() // 60)
    life = max(3, 180 // mins)                 # zones live ~3 hours
    confirm = 5 if mins == 1 else 2            # bars allowed after the first touch
    dyn, ghost = stop == "dinamico", stop == "ghost"
    max_risk = ({1: 100, 5: 150, 15: 200} if dyn else {1: 50, 5: 80, 15: 120}).get(mins, 150)
    back = max(3, lookback_min // mins)
    atr = mk.atr5() if dyn else None
    out, zs = [], []
    for i in range(2, len(b)):
        keep = []
        for z in zs:
            if i - z["born"] > life:
                continue
            bull, top, bot = z["bull"], z["top"], z["bot"]
            mid = (top + bot) / 2
            touched = L[i] <= top if bull else H[i] >= bot
            if z["touch"] is None and touched:
                z["touch"] = i
            if z["touch"] is not None:
                if (C[i] < bot) if bull else (C[i] > top):
                    continue                                   # crossed: dead
                if (C[i] > mid and C[i] > O[i]) if bull else (C[i] < mid and C[i] < O[i]):
                    if dyn:
                        lo, hi = L[max(0, i - back):i + 1].min(), H[max(0, i - back):i + 1].max()
                        buf = buffer * f[i]
                        px = min(z["origin"], lo) - buf if bull else max(z["origin"], hi) + buf
                        room = atr_mult * mk.asof("5min", atr, end[i])
                        if np.isfinite(room):
                            px = min(px, C[i] - room) if bull else max(px, C[i] + room)
                    else:
                        px = z["origin"] - f[i] if bull else z["origin"] + f[i]
                    if ghost:                          # the live bot: 20-35 pts of stop
                        d = min(max(abs(C[i] - px), 20 * f[i]), 35 * f[i])
                        px = C[i] - d if bull else C[i] + d
                    risk = (C[i] - px) if bull else (px - C[i])
                    if 3 * f[i] <= risk <= max_risk * f[i]:
                        out.append(Signal(end[i], bull, px, k=k))
                    continue                                   # one shot per zone
                if i - z["touch"] >= confirm:
                    continue
            keep.append(z)
        zs = keep
        gap_up, gap_dn = L[i] - H[i - 2], L[i - 2] - H[i]
        g_lo, g_hi = 3 * f[i], 60 * f[i]
        if g_lo <= gap_up <= g_hi:
            zs.append({"bull": True, "top": L[i], "bot": H[i - 2], "born": i, "touch": None,
                       "origin": min(L[i - 2], L[i - 1])})
        if g_lo <= gap_dn <= g_hi:
            zs.append({"bull": False, "top": L[i - 2], "bot": H[i], "born": i, "touch": None,
                       "origin": max(H[i - 2], H[i - 1])})
    return out


def orb(mk: Market, minutes: int, k: float, stop_at: str, max_stop: float | None = None,
        cap: str = "saltar", side: str | None = None, until: str | None = None,
        last: str = "15:30") -> list[Signal]:
    """Opening-range breakout: the high/low of the first `minutes` after
    09:30; the first 5-minute close outside it (before `last`), in that
    direction. Stop at the other side of the range (or its middle). One
    trade a day.

    max_stop: a stop farther than that from the breakout close either skips
    the day (cap="saltar") or is pulled in to that distance (cap="recortar").
    side: only "compra" or only "venta". until: flat at that time."""
    m1, b5 = mk.m1, mk.bars("5min")
    out = []
    rth = m1.between_time("09:30", "15:59")
    h, mnt = map(int, last.split(":"))
    for day, g in rth.groupby(rth.index.date):
        cut = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=30 + minutes)
        rng = g[g.index < cut]
        if len(rng) < minutes * 0.8:
            continue
        hi, lo = rng["high"].max(), rng["low"].min()
        f = float(mk.scale_at([cut])[0])             # 150 pts at 15,000 = 300 at 30,000
        if hi - lo > 150 * f:
            continue
        after = b5[(b5.index >= cut) & (b5.index < pd.Timestamp(day) + pd.Timedelta(hours=h, minutes=mnt))]
        for t, row in after.iterrows():
            if row["close"] > hi or row["close"] < lo:
                up = row["close"] > hi
                stop = (lo if up else hi) if stop_at == "opuesto" else (hi + lo) / 2
                if max_stop is not None and abs(row["close"] - stop) > max_stop * f:
                    if cap == "saltar":
                        break
                    stop = row["close"] - max_stop * f if up else row["close"] + max_stop * f
                if side is None or (side == "compra") == up:
                    out.append(Signal(row["end"].to_datetime64(), up, stop, k=k, until=until))
                break
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def pullback(mk: Market, tf: str, k: float, n: int = 20) -> list[Signal]:
    """Trend + pullback: the hourly trend (close vs a rising/falling 50-EMA on
    CLOSED hourly bars) sets the side; enter when a `tf` bar dips to its
    n-EMA and closes back in the trend's direction (a bounce off the EMA).
    Stop past the last 3 bars' extreme."""
    b, h1 = mk.bars(tf), mk.bars("1h")
    e50 = _ema(h1["close"].to_numpy(), 50)
    trend = np.where((h1["close"].to_numpy() > e50) & (e50 > np.roll(e50, 3)), 1,
                     np.where((h1["close"].to_numpy() < e50) & (e50 < np.roll(e50, 3)), -1, 0))
    known = pd.Series(trend, index=h1["end"].to_numpy())          # usable once the hour closed
    tr = known.reindex(b["end"].to_numpy(), method="ffill").fillna(0).to_numpy()
    O, H, L, C = (b[c].to_numpy() for c in ("open", "high", "low", "close"))
    e20, end = _ema(C, n), b["end"].to_numpy()
    out = []
    for i in range(3, len(b)):
        if tr[i] > 0 and L[i] <= e20[i] < C[i] and C[i] > O[i]:
            out.append(Signal(end[i], True, L[i - 2:i + 1].min() - 1, k=k))
        elif tr[i] < 0 and H[i] >= e20[i] > C[i] and C[i] < O[i]:
            out.append(Signal(end[i], False, H[i - 2:i + 1].max() + 1, k=k))
    return out


def vwap_revert(mk: Market, z: float) -> list[Signal]:
    """Mean reversion: price stretched z x ATR(14, 5m) away from the day's
    VWAP (from 09:30), then a reversal candle; target back at the VWAP."""
    b = mk.bars("5min")
    rth = b.between_time("09:30", "15:55")
    out = []
    for day, g in rth.groupby(rth.index.date):
        tp = (g["high"] + g["low"] + g["close"]) / 3
        vwap = (tp * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
        atr = (g["high"] - g["low"]).rolling(14, min_periods=6).mean()
        for (t, r), vw, a in zip(g.iterrows(), vwap, atr):
            if not (np.isfinite(vw) and np.isfinite(a)) or t.hour < 10 or t.hour >= 15:
                continue
            if r["close"] < vw - z * a and r["close"] > r["open"]:
                out.append(Signal(r["end"].to_datetime64(), True, r["low"] - 0.5 * a, target=vw))
            elif r["close"] > vw + z * a and r["close"] < r["open"]:
                out.append(Signal(r["end"].to_datetime64(), False, r["high"] + 0.5 * a, target=vw))
    return out


def boxes(mk: Market, n: int, k: float, vol: bool, tight: float) -> list[Signal]:
    """Box breakout ("draw the box, enter where it breaks"): the last `n`
    5-minute candles moved sideways, inside a range smaller than `tight`
    average candles. The first candle that CLOSES outside the box enters in
    that direction (with vol: only if its volume is 1.5x the box's average).
    Stop at the other side of the box; one breakout per box."""
    b = mk.bars("5min")
    O, H, L, C, V = (b[c].to_numpy() for c in ("open", "high", "low", "close", "volume"))
    t, end = b.index.to_numpy(), b["end"].to_numpy()
    avg = pd.Series(H - L).rolling(50, min_periods=20).mean().to_numpy()
    span = np.timedelta64(5 * n, "m")
    out, skip = [], -1
    for i in range(n, len(b)):
        if i <= skip or not np.isfinite(avg[i - 1]) or t[i] - t[i - n] != span:
            continue                                      # a gap (the daily pause) breaks the box
        hi, lo = H[i - n:i].max(), L[i - n:i].min()
        if hi - lo > tight * avg[i - 1]:
            continue
        up, dn = C[i] > hi, C[i] < lo
        if not (up or dn):
            continue
        if vol and V[i] < 1.5 * V[i - n:i].mean():
            continue
        out.append(Signal(end[i], bool(up), lo - 1 if up else hi + 1, k=k))
        skip = i + n                                      # the next box starts after this one
    return out


def levels(mk: Market, which: str, k: float, vol: bool) -> list[Signal]:
    """Breakout of yesterday's regular-session high/low (which="ayer") or of
    the night session's high/low (which="noche", 18:00 → 09:30): the first
    5-minute close beyond the level between 09:30 and 15:30 (not a candle
    that opened 20+ pts past it), once per side and day. Stop behind that
    candle; with vol, its volume must be 1.5x the
    last hour's average."""
    m1, b = mk.m1, mk.bars("5min")
    tday = (m1.index + pd.Timedelta(hours=6)).normalize()
    mins = m1.index.hour * 60 + m1.index.minute
    rth = (mins >= 9 * 60 + 30) & (mins < 16 * 60)
    night = (mins < 9 * 60 + 30) | (mins >= 18 * 60)
    g_rth = m1[rth].groupby(tday[rth]).agg({"high": "max", "low": "min"})
    g_night = m1[night].groupby(tday[night]).agg({"high": "max", "low": "min"})
    ref = g_rth.shift(1) if which == "ayer" else g_night
    ref = ref.reindex(g_rth.index)
    bt = b.index
    bday = (bt + pd.Timedelta(hours=6)).normalize()
    bmin = bt.hour * 60 + bt.minute
    O, H, L, C, V = (b[c].to_numpy() for c in ("open", "high", "low", "close", "volume"))
    vavg = pd.Series(V).rolling(12, min_periods=6).mean().shift(1).to_numpy()
    end = b["end"].to_numpy()
    lv = ref.reindex(bday)
    lhi, llo = lv["high"].to_numpy(), lv["low"].to_numpy()
    ok = np.asarray((bmin >= 9 * 60 + 30) & (bmin < 15 * 60 + 30))
    out, done = [], set()
    for i in np.flatnonzero(ok & np.isfinite(lhi)):
        if vol and not (np.isfinite(vavg[i]) and V[i] >= 1.5 * vavg[i]):
            continue
        d = bday[i]
        if C[i] > lhi[i] and O[i] <= lhi[i] + 20 and (d, 1) not in done:
            done.add((d, 1))
            out.append(Signal(end[i], True, L[i] - 1, k=k))
        elif C[i] < llo[i] and O[i] >= llo[i] - 20 and (d, -1) not in done:
            done.add((d, -1))
            out.append(Signal(end[i], False, H[i] + 1, k=k))
    return out


# --------------------------------------------------------------------------- #
# filters: keep a signal only when the wider market agrees with its side
# --------------------------------------------------------------------------- #
def nq_trend(mk: Market, n: int = 20):
    """+1 while NQ's last 5-minute close is above its n-EMA, -1 below."""
    b = mk.bars("5min")
    c = b["close"].to_numpy()
    side = np.sign(c - _ema(c, n))

    def f(when) -> float:
        return mk.asof("5min", side, when)
    return f


def _unsplit(df: pd.DataFrame) -> pd.DataFrame:
    """Undo stock splits in raw exchange prices (a 10:1 split looks like a
    90% overnight drop): scale everything before the jump."""
    c = df["close"]
    day = df.index.normalize()
    last = c.groupby(day).last()
    first = df["open"].groupby(day).first()
    ratio = (first / last.shift(1)).dropna()
    for d, r in ratio.items():
        if r < 0.6 or r > 1.7:
            f = round(1 / r) if r < 1 else 1 / round(r)
            before = df.index < d
            df.loc[before, ["open", "high", "low", "close"]] /= f
    return df


def load_megacaps(start: str, end: str, symbols=MEGACAPS) -> pd.DataFrame | None:
    """Regular-session 5-minute closes of the big Nasdaq names, one column
    each (None when their data was never downloaded)."""
    cols = {}
    for sym in symbols:
        csv = ROOT / "market_data" / sym / "DATABENTO_1m.csv"
        if not csv.exists():
            continue
        m1 = _unsplit(load_1m(sym, "DATABENTO", start, end).between_time("09:30", "15:59").copy())
        cols[sym] = m1["close"].resample("5min", label="left", closed="left").last().dropna()
    if len(cols) < max(4, len(symbols) // 2):
        return None
    return pd.DataFrame(cols)


def breadth(mk: Market, closes: pd.DataFrame, n: int = 20, need: float = 0.75):
    """+1 when at least `need` of the big names are above their own n-EMA
    (5-minute bars, regular session), -1 when that many are below, 0 in
    between or when there is no fresh stock data (night, holidays)."""
    above = pd.DataFrame({s: (c > c.ewm(span=n, adjust=False).mean()).astype(float)
                          .where(c.notna()) for s, c in closes.items()})
    share = above.mean(axis=1, skipna=True)
    count = above.notna().sum(axis=1)
    share[count < max(4, closes.shape[1] // 2)] = np.nan
    ends = (share.index + pd.Timedelta("5min")).to_numpy()
    vals = share.to_numpy()

    def f(when) -> float:
        i = int(np.searchsorted(ends, np.datetime64(when), side="right")) - 1
        if i < 0 or np.datetime64(when) - ends[i] > np.timedelta64(10, "m") or not np.isfinite(vals[i]):
            return 0.0
        return 1.0 if vals[i] >= need else (-1.0 if vals[i] <= 1 - need else 0.0)
    return f


def only_with(signals: list[Signal], side_fn) -> list[Signal]:
    """The signals whose side matches the filter at the moment they appear."""
    return [s for s in signals if side_fn(s.ready) == (1.0 if s.long else -1.0)]


# the rows closest to the live bot (general grid / zones study)
BOT_LIKE = ("Zonas 1 min · TP 3× stop · BE+2 al 70%",
            "Zonas 1 min · stop 20–35 pts (como GHOST) · TP 3× · BE+2")


def variants_zonas(mk: Market):
    """GHOST's zone strategy on 1-, 5- and 15-minute candles, with its own
    20-35 pt stop, the stop behind the origin, or the dynamic stop. With
    --escala precio every point limit grows with the price."""
    names = {"ghost": "stop 20–35 pts (como GHOST)", "origen": "stop detrás del origen",
             "dinamico": "stop dinámico +3"}
    for tf, lab in (("1min", "1 min"), ("5min", "5 min"), ("15min", "15 min")):
        for stop in ("ghost", "origen", "dinamico"):
            for k in (2.0, 3.0):
                sig = zones(mk, tf, k, stop=stop)
                yield f"Zonas {lab} · {names[stop]} · TP {k:g}× · BE+2", sig, 0.7
                if stop == "ghost" and k == 3.0:
                    yield f"Zonas {lab} · {names[stop]} · TP {k:g}×", sig, None


def variants(mk: Market, big: pd.DataFrame | None = None):
    """(name, signals, BE rule) — a small grid per idea, so a ✔ is not just
    the one lucky setting out of hundreds."""
    memo: dict = {}

    def once(fn, *args, **kw):                 # each signal list is built only once
        key = (fn.__name__, args, tuple(sorted(kw.items())))
        if key not in memo:
            memo[key] = fn(mk, *args, **kw)
        return memo[key]

    zones_, boxes_, levels_ = (lambda *a, **k: once(zones, *a, **k),
                               lambda *a, **k: once(boxes, *a, **k),
                               lambda *a, **k: once(levels, *a, **k))
    for tf, lab in (("1min", "1 min"), ("5min", "5 min"), ("15min", "15 min")):
        for k in (2.0, 3.0):
            sig = zones_(tf, k)
            yield f"Zonas {lab} · TP {k:g}× stop", sig, None
            yield f"Zonas {lab} · TP {k:g}× stop · BE+2 al 70%", sig, 0.7
    # the stop behind the level just tested (and wider in a busy market)
    for tf, lab in (("1min", "1 min"), ("5min", "5 min")):
        for buf in (3.0, 5.0):
            sig = zones_(tf, 3.0, stop="dinamico", buffer=buf)
            yield f"Zonas {lab} · stop dinámico +{buf:g} · TP 3×", sig, None
            yield f"Zonas {lab} · stop dinámico +{buf:g} · TP 3× · BE+2 al 70%", sig, 0.7
    for minutes in (15, 30):
        for stop_at in ("opuesto", "medio"):
            for k in (1.5, 2.0):
                yield (f"Ruptura apertura {minutes} min · stop {stop_at} · TP {k:g}×",
                       orb(mk, minutes, k, stop_at), None)
    for n, tight in ((6, 2.5), (12, 3.5)):
        for vol in (False, True):
            yield (f"Caja {n * 5} min · ruptura{' con volumen' if vol else ''} · TP 2×",
                   boxes_(n, 2.0, vol, tight), None)
    for which in ("ayer", "noche"):
        for vol in (False, True):
            yield (f"Rompe máx/mín de {which}{' con volumen' if vol else ''} · TP 2×",
                   levels_(which, 2.0, vol), None)
    for tf, lab in (("5min", "5 min"), ("15min", "15 min")):
        for k in (2.0, 3.0):
            yield f"Tendencia + rebote EMA 20 {lab} · TP {k:g}×", pullback(mk, tf, k), None
    for n in (9, 50, 200):
        yield f"Tendencia + rebote EMA {n} 5 min · TP 2×", pullback(mk, "5min", 2.0, n), None
    for z in (1.5, 2.5):
        yield f"Vuelta al VWAP · {z:g}× ATR", vwap_revert(mk, z), None

    # the same entries, only on the side the wider market agrees with
    bases = [("Zonas 1 min · TP 3× · BE+2 al 70%", zones_("1min", 3.0), 0.7),
             ("Zonas 5 min · TP 3×", zones_("5min", 3.0), None),
             ("Caja 60 min · ruptura · TP 2×", boxes_(12, 2.0, False, 3.5), None),
             ("Rompe máx/mín de ayer · TP 2×", levels_("ayer", 2.0, False), None)]
    nq = nq_trend(mk, 20)
    for name, sig, be in bases:
        yield f"{name} · solo si NQ va a favor (EMA 20)", only_with(sig, nq), be
    if big is not None:
        grandes = breadth(mk, big, 20)
        for name, sig, be in bases:
            yield f"{name} · solo si las grandes van a favor", only_with(sig, grandes), be


def variants_orb(mk: Market):
    """A closer look at the opening-range breakout: are its neighbours good
    too (range length, target), and can it fit an Apex account (smaller
    stop, earlier exit, one side only)?"""
    for minutes in (5, 10, 15, 20, 30):
        for k in (1.0, 1.5, 2.0, 3.0):
            yield f"Apertura {minutes} min · TP {k:g}×", orb(mk, minutes, k, "opuesto"), None
    for k in (1.5, 2.0):
        for cap_pts in (60, 80):
            yield (f"Apertura 15 min · TP {k:g}× · sin días de stop > {cap_pts} pts",
                   orb(mk, 15, k, "opuesto", max_stop=cap_pts), None)
        yield (f"Apertura 15 min · TP {k:g}× · stop recortado a 50 pts",
               orb(mk, 15, k, "opuesto", max_stop=50, cap="recortar"), None)
        for until in ("12:00", "14:00"):
            yield f"Apertura 15 min · TP {k:g}× · cierra a las {until}", \
                orb(mk, 15, k, "opuesto", until=until), None
        yield f"Apertura 15 min · TP {k:g}× · entrada solo antes de 11:00", \
            orb(mk, 15, k, "opuesto", last="11:00"), None
        yield f"Apertura 15 min · TP {k:g}× · BE+2 al 70%", orb(mk, 15, k, "opuesto"), 0.7
    for side in ("compra", "venta"):
        yield f"Apertura 15 min · TP 1.5× · solo {side}", orb(mk, 15, 1.5, "opuesto", side=side), None


def streak(p: list[float]) -> tuple[int, float]:
    """Longest run of losing trades in a row, and what it cost."""
    best, cur, cost, run_cost = 0, 0, 0.0, 0.0
    for x in p:
        if x < 0:
            cur, run_cost = cur + 1, run_cost + x
            if cur > best:
                best, cost = cur, run_cost
        else:
            cur, run_cost = 0, 0.0
    return best, cost


def apex_table(rows, raws: dict, apex_dd: float, extra_cost: float = 0.0) -> None:
    """With 1 and 2 MNQ: net, per year, worst drop and worst losing streak,
    against the account's allowed drawdown. `extra_cost`: points to add to
    each trade's cost when the run was priced for another contract (1 NQ)."""
    print(f"\n== ¿Cabe en la cuenta? (peor caída contra el límite de ${apex_dd:,.0f}) ==")
    print(f"  {'':<56} {'MNQ':>3} {'neto':>11} {'por año':>9} {'peor caída':>11} "
          f"{'racha mala':>16}")
    good = [r for r in rows if r["ok"]] or sorted(
        (r for r in rows if r["test"]["n"]), key=lambda r: -r["test"]["conf"])[:5]
    for r in sorted(good, key=lambda r: -r["test"]["conf"])[:10]:
        days = raws[r["name"]]
        pts = [p - extra_cost for _, p, *_ in days]
        span = (pd.Timestamp(days[-1][0]) - pd.Timestamp(days[0][0])).days / 365.25 if days else 1
        for n in (1, 2):
            usd_ = [p * 2.0 * n for p in pts]
            st = stats(usd_)
            ln, lc = streak(usd_)
            fits = "✔" if st["dd"] <= apex_dd and st["net"] > 0 else "✘"
            print(f"  {fits} {r['name'][:54]:<54} {n:>3} {usd(st['net'], 1):>11} "
                  f"{usd(st['net'] / max(span, 0.5), 1):>9} {usd(-st['dd'], 1):>11} "
                  f"{ln:>3} seguidas {usd(lc, 1):>7}")
    print("  (✔ = gana y su peor caída cabe en el límite; la cuenta real mide también lo"
          " no realizado, así que deja margen)")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def stats(p: list[float]) -> dict:
    a = np.asarray(p, float)
    if not len(a):
        return {"n": 0}
    eq = np.cumsum(a)
    dd = float(np.max(np.maximum.accumulate(np.r_[0.0, eq])[1:] - eq)) if len(a) else 0.0
    sd = a.std(ddof=1) if len(a) > 1 else 0.0
    return {"n": len(a), "wr": float((a > 0).mean()), "avg": float(a.mean()),
            "net": float(a.sum()), "dd": dd,
            "conf": float(a.mean() / (sd / math.sqrt(len(a)))) if sd > 0 else 0.0}


def usd(pts: float, pv: float) -> str:
    return f"{'+' if pts >= 0 else '−'}${abs(pts * pv):,.0f}"


def sized_usd(trades, position: str, pv: float, risk_usd: float) -> list[tuple]:
    """[(date, net $, stop pts, *rest)] from run()'s (date, points, stop, *rest).
    A fixed position: points x its $/pt. "riesgo": as many MNQ as fit the
    stop in risk_usd (at most MAX_MICROS); the costs in points are the same
    per micro, so every micro adds the same result."""
    if position != "riesgo":
        return [(d, p * pv, risk, *rest) for d, p, risk, *rest in trades]
    out = []
    for d, p, risk, *rest in trades:
        n = min(MAX_MICROS, int(risk_usd // (risk * pv)))
        if n >= 1:
            out.append((d, n * p * pv, risk, *rest))
    return out


def by_hour(trades) -> dict[int, dict]:
    """Per entry hour: trades, win rate, net $, average stop (pts). `trades`
    are run()'s tuples already sized: (date, $, stop pts, hour)."""
    hours: dict[int, list] = {}
    for d, usd_, risk, hour in trades:
        hours.setdefault(hour, []).append((usd_, risk))
    return {h: {**stats([u for u, _ in v]), "stop": float(np.mean([r for _, r in v]))}
            for h, v in sorted(hours.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--data", default="DATABENTO")
    ap.add_argument("--start", default="2021-09-27")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y-%m-%d"))
    ap.add_argument("--split", default="2024-01-01")
    ap.add_argument("--position", choices=sorted(POSITIONS), default="5mnq")
    ap.add_argument("--risk-usd", type=float, default=300.0,
                    help="con --position riesgo: dólares que arriesga cada stop")
    ap.add_argument("--slip-ticks", type=float, default=1.0, help="deslizamiento por lado")
    ap.add_argument("--sessions", choices=("rth", "all"), default="rth")
    ap.add_argument("--min-n", type=int, default=60, help="trades mínimos en TRAIN y en TEST")
    ap.add_argument("--estudio", choices=("todo", "apertura", "zonas"), default="todo",
                    help="apertura: la ruptura de apertura a fondo · zonas: la estrategia de "
                         "GHOST en velas de 1, 5 y 15 min")
    ap.add_argument("--escala", choices=("puntos", "precio"), default="puntos",
                    help="precio: los límites en puntos crecen con el precio del NQ "
                         f"(escritos para {REF_PRICE:,.0f}; a 30,000 valen el doble)")
    ap.add_argument("--apex-dd", type=float, default=2500.0,
                    help="caída máxima que permite la cuenta (Apex 50K ≈ $2,500)")
    ap.add_argument("--json-out")
    a = ap.parse_args()

    label, pv, comm = POSITIONS[a.position]
    cost = comm / pv + 2 * a.slip_ticks * TICK
    riesgo = a.position == "riesgo"
    if riesgo:
        label = f"riesgo fijo ${a.risk_usd:,.0f} por trade (1 a {MAX_MICROS} MNQ)"
    t0 = time.time()
    mk = Market(load_1m(a.symbol, a.data, a.start, a.end))
    mk.scaled = a.escala == "precio"
    big = load_megacaps(a.start, a.end) if a.estudio == "todo" else None
    print(f"== Laboratorio · {a.symbol} {a.start} → {a.end} · {label} · costo "
          f"{cost:.2f} pts/trade ({'por MNQ ' if riesgo else ''}${cost * pv:,.2f}) · entradas "
          f"{'09:30-15:30' if a.sessions == 'rth' else 'día y noche'} · TRAIN < {a.split} ≤ TEST")
    print("   límites en puntos " + (f"ESCALADOS al precio (escritos para NQ a {REF_PRICE:,.0f})"
                                     if mk.scaled else "fijos"))
    print("   prom = dólares por trade después de costos · conf = qué tan lejos del azar"
          " (≥ 2 ≈ poco probable por suerte)")
    if big is None and a.estudio == "todo":
        print("   (sin datos de las grandes —NVDA, MSFT, AAPL...—: se salta ese filtro. Para "
              "bajarlos: scripts/fetch_databento.py --stocks grandes)")
    print()
    # the same entries sized the other way, to compare fixed contracts with fixed $ risk
    alt = "5mnq" if riesgo else "riesgo"
    alt_pv, alt_label = POSITIONS[alt][1], ("5 MNQ fijos" if riesgo else f"riesgo fijo ${a.risk_usd:,.0f}")
    rows, raws = [], {}
    grid = {"apertura": lambda: variants_orb(mk),
            "zonas": lambda: variants_zonas(mk)}.get(a.estudio, lambda: variants(mk, big))()
    for name, sig, be in grid:
        raw = run(mk, sig, cost, a.sessions, be_at=be,
                  max_risk=a.risk_usd / pv if riesgo else None)
        raws[name] = raw
        trades = sized_usd(raw, a.position, pv, a.risk_usd)
        tr = stats([t[1] for t in trades if t[0] < a.split])
        te = stats([t[1] for t in trades if t[0] >= a.split])
        # a ✔ must earn money in BOTH periods and not by a hair (conf ≥ 1 in each)
        ok = (tr["n"] >= a.min_n and te["n"] >= a.min_n and tr["avg"] > 0 and te["avg"] > 0
              and tr["conf"] >= 1.0 and te["conf"] >= 1.0)
        years: dict[str, list[float]] = {}
        for t in trades:
            years.setdefault(t[0][:4], []).append(t[1])
        hours = by_hour(trades)
        rows.append({"name": name, "ok": ok, "train": tr, "test": te,
                     "all": stats([t[1] for t in trades]),
                     "alt": stats([t[1] for t in sized_usd(raw, alt, alt_pv, a.risk_usd)]),
                     "years": {y: stats(v) for y, v in sorted(years.items())},
                     "hours": hours})
        f = lambda s: (f"{s['n']:>5} gana {s['wr']:>4.0%} prom {usd(s['avg'], 1):>6}"   # noqa: E731
                       f" neto {usd(s['net'], 1):>9} conf {s['conf']:+.1f}") if s["n"] else "    0"
        print(f"{'✔' if ok else '✘'} {name:<62} TRAIN {f(tr)}   TEST {f(te)}", flush=True)
    good = [r for r in rows if r["ok"]]
    print(f"\n→ Ganan en TRAIN y en TEST (conf ≥ 1 en ambos): {len(good)} de {len(rows)}. "
          f"Por pura suerte pasarían ~{len(rows) * 0.025:.0f}: mira 'conf' y que gane casi todos "
          "los años.")
    for r in sorted(good, key=lambda r: -r["test"]["conf"])[:6]:
        s = r["all"]
        print(f"\n  {r['name']}: {s['n']} trades · neto {usd(s['net'], 1)} · peor caída "
              f"{usd(-s['dd'], 1)}")
        for y, ys in r["years"].items():
            print(f"    {y}: {ys['n']:>4} trades  neto {usd(ys['net'], 1):>9}  gana {ys['wr']:.0%}")

    # by hour: the bot's own idea, the ✔ ones (or the best in TEST if none)
    bot = [r for r in rows if r["name"] in BOT_LIKE]
    picks = bot + (sorted(good, key=lambda r: -r["test"]["conf"])[:5] or
                   sorted((r for r in rows if r["test"]["n"]), key=lambda r: -r["test"]["conf"])[:3])
    print("\n== Por hora de entrada (todo el periodo): cuánto deja cada hora y qué tan grande es el stop ==")
    seen = set()
    for r in picks:
        if r["name"] in seen or not r["hours"]:
            continue
        seen.add(r["name"])
        print(f"\n  {r['name']}{'  (lo más parecido al bot)' if r['name'] in BOT_LIKE else ''}")
        for h, hs in r["hours"].items():
            print(f"    {h:02d}h  {hs['n']:>5} trades  gana {hs['wr']:>4.0%}  neto {usd(hs['net'], 1):>9}"
                  f"  prom {usd(hs['avg'], 1):>6}  stop prom {hs['stop']:5.1f} pts")

    # fixed contracts vs fixed $ risk, same entries
    print(f"\n== {label} vs {alt_label} (mismas entradas, todo el periodo) ==")
    print(f"  {'':<62} {'neto':>10} {'peor caída':>11}   {'neto':>10} {'peor caída':>11}")
    top = sorted(rows, key=lambda r: -max(r["all"].get("net", -1e18), r["alt"].get("net", -1e18)))[:15]
    for r in bot + [r for r in top if r not in bot]:
        s, o = r["all"], r["alt"]
        if not s["n"] and not o["n"]:
            continue
        cell = lambda x: (f"{usd(x['net'], 1):>10} {usd(-x['dd'], 1):>11}" if x["n"]   # noqa: E731
                          else f"{'—':>10} {'—':>11}")
        print(f"  {r['name']:<62} {cell(s)}   {cell(o)}")
    print(f"  (izquierda: {label} · derecha: {alt_label})")
    mnq_cost = POSITIONS["riesgo"][2] / POSITIONS["riesgo"][1] + 2 * a.slip_ticks * TICK
    apex_table(rows, raws, a.apex_dd, extra_cost=mnq_cost - cost)
    print(f"\n({time.time() - t0:.0f} s)")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({"meta": {**vars(a), "cost_pts": cost, "pv": pv,
                                                          "label": label}, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
