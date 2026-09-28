"""Strategy lab: try simple, DIFFERENT trading ideas on years of 1-minute NQ in
minutes (the full bot replay takes a night), with the same blind test as
edge_finder: every variant is judged on TRAIN (before --split) and must also
make money on TEST (from --split on) to get a ✔.

    .venv/bin/python scripts/strategy_lab.py                       # 2021-09 → today
    .venv/bin/python scripts/strategy_lab.py --position 1nq --sessions all

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
# $ per point and round-trip commission (Tradovate at Apex, per position)
POSITIONS = {"5mnq": ("5 MNQ", 10.0, 5 * 1.04), "1nq": ("1 NQ", 20.0, 3.10)}


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


def _minutes(ts) -> int:
    ts = pd.Timestamp(ts)
    return ts.hour * 60 + ts.minute


def entry_open(ts, sessions: str) -> bool:
    m = _minutes(ts)
    if sessions == "rth":
        return 9 * 60 + 30 <= m <= 15 * 60 + 30
    return not (16 * 60 + 15 <= m < 18 * 60)


def run(mk: Market, signals: list[Signal], cost_pts: float, sessions: str,
        be_at: float | None = None, be_plus: float = 2.0) -> list[tuple[str, float]]:
    """One position at a time. Returns [(entry date, net points)]."""
    out, busy = [], 0
    for s in sorted(signals, key=lambda x: x.ready):
        i0 = int(np.searchsorted(mk.t, s.ready))
        if i0 < busy or i0 >= len(mk.t) or not entry_open(mk.t[i0], sessions):
            continue
        end = int(mk.eod[i0])
        if end <= i0:
            continue
        entry = mk.o[i0]
        risk = (entry - s.stop) if s.long else (s.stop - entry)
        if risk <= TICK:
            continue
        tgt = s.target if s.target is not None else (
            entry + s.k * risk if s.long else entry - s.k * risk)
        if (tgt - entry) * (1 if s.long else -1) <= TICK:
            continue
        j, px = _exit(mk, i0, end, s.long, s.stop, tgt, entry, be_at, be_plus)
        pts = (px - entry) if s.long else (entry - px)
        out.append((str(pd.Timestamp(mk.t[i0]).date()), pts - cost_pts))
        busy = j + 1
    return out


def _first(mask: np.ndarray) -> int:
    if not len(mask):
        return -1
    k = int(np.argmax(mask))
    return k if mask[k] else -1


def _exit(mk, i0, end, long, stop, tgt, entry, be_at, be_plus):
    h, l, o = mk.h[i0:end], mk.l[i0:end], mk.o[i0:end]
    sgn = 1 if long else -1

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

    if be_at is not None:
        trig = entry + sgn * be_at * abs(tgt - entry)
        hit = _first((h >= trig) if long else (l <= trig))
        first = scan(stop, 0)
        if hit >= 0 and (first is None or first[0] > hit):
            res = scan(entry + sgn * be_plus, hit + 1)      # stop moves from the next minute
            if res is None:
                return end - 1, mk.c[end - 1]
            return i0 + res[0], res[1]
        if first is not None:
            return i0 + first[0], first[1]
        return end - 1, mk.c[end - 1]
    res = scan(stop, 0)
    if res is None:
        return end - 1, mk.c[end - 1]                      # 16:55 flat
    return i0 + res[0], res[1]


# --------------------------------------------------------------------------- #
# strategies (each returns signals; the runner decides entries and exits)
# --------------------------------------------------------------------------- #
def zones(mk: Market, tf: str, k: float) -> list[Signal]:
    """The bot's idea on any timeframe: a fresh fair-value gap, the FIRST
    return into it, and a candle that closes back past the zone's middle in
    the gap's direction. Stop behind the move's origin."""
    b = mk.bars(tf)
    O, H, L, C = (b[c].to_numpy() for c in ("open", "high", "low", "close"))
    end = b["end"].to_numpy()
    mins = int(pd.Timedelta(tf).total_seconds() // 60)
    life = max(3, 180 // mins)                 # zones live ~3 hours
    confirm = 5 if mins == 1 else 2            # bars allowed after the first touch
    max_risk = {1: 50, 5: 80, 15: 120}.get(mins, 150)
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
                    risk = (C[i] - z["origin"]) if bull else (z["origin"] - C[i])
                    if 3 <= risk <= max_risk:
                        out.append(Signal(end[i], bull, z["origin"], k=k))
                    continue                                   # one shot per zone
                if i - z["touch"] >= confirm:
                    continue
            keep.append(z)
        zs = keep
        gap_up, gap_dn = L[i] - H[i - 2], L[i - 2] - H[i]
        if 3 <= gap_up <= 60:
            zs.append({"bull": True, "top": L[i], "bot": H[i - 2], "born": i, "touch": None,
                       "origin": min(L[i - 2], L[i - 1]) - 1})
        if 3 <= gap_dn <= 60:
            zs.append({"bull": False, "top": L[i - 2], "bot": H[i], "born": i, "touch": None,
                       "origin": max(H[i - 2], H[i - 1]) + 1})
    return out


def orb(mk: Market, minutes: int, k: float, stop_at: str) -> list[Signal]:
    """Opening-range breakout: the high/low of the first `minutes` after
    09:30; the first 5-minute close outside it, in that direction. Stop at
    the other side of the range (or its middle). One trade a day."""
    m1, b5 = mk.m1, mk.bars("5min")
    out = []
    rth = m1.between_time("09:30", "15:59")
    for day, g in rth.groupby(rth.index.date):
        cut = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=30 + minutes)
        rng = g[g.index < cut]
        if len(rng) < minutes * 0.8:
            continue
        hi, lo = rng["high"].max(), rng["low"].min()
        if hi - lo > 150:
            continue
        after = b5[(b5.index >= cut) & (b5.index < pd.Timestamp(day) + pd.Timedelta(hours=15, minutes=30))]
        for t, row in after.iterrows():
            if row["close"] > hi or row["close"] < lo:
                up = row["close"] > hi
                stop = (lo if up else hi) if stop_at == "opuesto" else (hi + lo) / 2
                out.append(Signal(row["end"].to_datetime64(), up, stop, k=k))
                break
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def pullback(mk: Market, tf: str, k: float) -> list[Signal]:
    """Trend + pullback: the hourly trend (close vs a rising/falling 50-EMA on
    CLOSED hourly bars) sets the side; enter when a `tf` bar dips to its
    20-EMA and closes back in the trend's direction. Stop past the last 3
    bars' extreme."""
    b, h1 = mk.bars(tf), mk.bars("1h")
    e50 = _ema(h1["close"].to_numpy(), 50)
    trend = np.where((h1["close"].to_numpy() > e50) & (e50 > np.roll(e50, 3)), 1,
                     np.where((h1["close"].to_numpy() < e50) & (e50 < np.roll(e50, 3)), -1, 0))
    known = pd.Series(trend, index=h1["end"].to_numpy())          # usable once the hour closed
    tr = known.reindex(b["end"].to_numpy(), method="ffill").fillna(0).to_numpy()
    O, H, L, C = (b[c].to_numpy() for c in ("open", "high", "low", "close"))
    e20, end = _ema(C, 20), b["end"].to_numpy()
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


def variants(mk: Market):
    """(name, signals builder, BE rule) — a small grid per idea, so a ✔ is
    not just the one lucky setting out of hundreds."""
    for tf, lab in (("1min", "1 min"), ("5min", "5 min"), ("15min", "15 min")):
        for k in (2.0, 3.0):
            sig = zones(mk, tf, k)
            yield f"Zonas {lab} · TP {k:g}× stop", sig, None
            yield f"Zonas {lab} · TP {k:g}× stop · BE+2 al 70%", sig, 0.7
    for minutes in (15, 30):
        for stop_at in ("opuesto", "medio"):
            for k in (1.5, 2.0):
                yield (f"Ruptura apertura {minutes} min · stop {stop_at} · TP {k:g}×",
                       orb(mk, minutes, k, stop_at), None)
    for tf, lab in (("5min", "5 min"), ("15min", "15 min")):
        for k in (2.0, 3.0):
            yield f"Tendencia + retroceso {lab} · TP {k:g}×", pullback(mk, tf, k), None
    for z in (1.5, 2.5):
        yield f"Vuelta al VWAP · {z:g}× ATR", vwap_revert(mk, z), None


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--data", default="DATABENTO")
    ap.add_argument("--start", default="2021-09-27")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y-%m-%d"))
    ap.add_argument("--split", default="2024-01-01")
    ap.add_argument("--position", choices=sorted(POSITIONS), default="5mnq")
    ap.add_argument("--slip-ticks", type=float, default=1.0, help="deslizamiento por lado")
    ap.add_argument("--sessions", choices=("rth", "all"), default="rth")
    ap.add_argument("--min-n", type=int, default=60, help="trades mínimos en TRAIN y en TEST")
    ap.add_argument("--json-out")
    a = ap.parse_args()

    label, pv, comm = POSITIONS[a.position]
    cost = comm / pv + 2 * a.slip_ticks * TICK
    t0 = time.time()
    mk = Market(load_1m(a.symbol, a.data, a.start, a.end))
    print(f"== Laboratorio · {a.symbol} {a.start} → {a.end} · {label} (${pv:g}/pt) · costo "
          f"{cost:.2f} pts/trade ({usd(cost, pv)[1:]}) · entradas "
          f"{'09:30-15:30' if a.sessions == 'rth' else 'día y noche'} · TRAIN < {a.split} ≤ TEST")
    print("   prom = dólares por trade después de costos · conf = qué tan lejos del azar"
          " (≥ 2 ≈ poco probable por suerte)\n")
    rows = []
    for name, sig, be in variants(mk):
        trades = run(mk, sig, cost, a.sessions, be_at=be)
        tr = stats([p for d, p in trades if d < a.split])
        te = stats([p for d, p in trades if d >= a.split])
        # a ✔ must earn money in BOTH periods and not by a hair (conf ≥ 1 in each)
        ok = (tr["n"] >= a.min_n and te["n"] >= a.min_n and tr["avg"] > 0 and te["avg"] > 0
              and tr["conf"] >= 1.0 and te["conf"] >= 1.0)
        years: dict[str, list[float]] = {}
        for d, p in trades:
            years.setdefault(d[:4], []).append(p)
        rows.append({"name": name, "ok": ok, "train": tr, "test": te,
                     "all": stats([p for _, p in trades]),
                     "years": {y: stats(v) for y, v in sorted(years.items())}})
        f = lambda s: (f"{s['n']:>5} gana {s['wr']:>4.0%} prom {usd(s['avg'], pv):>6}"   # noqa: E731
                       f" neto {usd(s['net'], pv):>9} conf {s['conf']:+.1f}") if s["n"] else "    0"
        print(f"{'✔' if ok else '✘'} {name:<46} TRAIN {f(tr)}   TEST {f(te)}")
    good = [r for r in rows if r["ok"]]
    print(f"\n→ Ganan en TRAIN y en TEST (conf ≥ 1 en ambos): {len(good)} de {len(rows)}. "
          "Con tantas pruebas alguna puede pasar por suerte: mira 'conf' y que gane casi todos los años.")
    for r in sorted(good, key=lambda r: -r["test"]["conf"])[:5]:
        s = r["all"]
        print(f"\n  {r['name']}: {s['n']} trades · neto {usd(s['net'], pv)} · peor caída "
              f"{usd(-s['dd'], pv)}")
        for y, ys in r["years"].items():
            print(f"    {y}: {ys['n']:>4} trades  neto {usd(ys['net'], pv):>9}  gana {ys['wr']:.0%}")
    print(f"\n({time.time() - t0:.0f} s)")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({"meta": {**vars(a), "cost_pts": cost, "pv": pv,
                                                          "label": label}, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
