"""Generate small, deterministic sample CSVs for two ES contracts, including
overnight (ETH) and regular (RTH) bars, plus a deliberately-invalid file to
demonstrate validation. Output layout matches CsvProvider:

    tests/data_samples/ES/ESM26_1m.csv
    tests/data_samples/ES/ESU26_1m.csv
    tests/data_samples/ES/BAD_1m.csv      (invalid rows for validation demo)

Timestamps are naive US/Eastern at the START of each 1-minute bar
(FirstRateData convention).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "tests" / "data_samples" / "ES"


def trade_dates(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def day_open_times(d: date) -> pd.DatetimeIndex:
    """Open-labeled 1m timestamps for one trade date: overnight ETH + RTH + post."""
    prev = d - timedelta(days=1)
    overnight = pd.date_range(datetime.combine(prev, time(18, 0)),
                              datetime.combine(d, time(9, 30)), freq="1min", inclusive="left")
    rth = pd.date_range(datetime.combine(d, time(9, 30)),
                        datetime.combine(d, time(16, 0)), freq="1min", inclusive="left")
    post = pd.date_range(datetime.combine(d, time(16, 0)),
                         datetime.combine(d, time(17, 0)), freq="1min", inclusive="left")
    return overnight.append(rth).append(post)


def build_contract(dates: list[date], start_price: float, seed: int,
                   day_volume_by_idx) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    price = start_price
    for i, d in enumerate(dates):
        times = day_open_times(d)
        n = len(times)
        opens, highs, lows, closes = [], [], [], []
        for _ in range(n):
            o = price
            c = o + float(rng.normal(0, 1.0))
            h = max(o, c) + abs(float(rng.normal(0, 0.3)))
            l = min(o, c) - abs(float(rng.normal(0, 0.3)))
            opens.append(round(o, 2)); highs.append(round(h, 2))
            lows.append(round(l, 2)); closes.append(round(c, 2))
            price = c
        vol = day_volume_by_idx(i) / n
        frames.append(pd.DataFrame({
            "datetime": times, "open": opens, "high": highs,
            "low": lows, "close": closes, "volume": round(vol, 2),
        }))
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    dates = trade_dates(date(2026, 6, 8), 3)

    # front contract: heavy volume days 0-1, lighter day 2 (rolling out)
    front = build_contract(dates, 5000.0, seed=1,
                           day_volume_by_idx=lambda i: [3000, 2500, 800][i])
    # back contract: light early, overtakes on day 2 (roll date)
    back = build_contract(dates, 5000.0 * 1.015, seed=2,
                          day_volume_by_idx=lambda i: [400, 900, 3200][i])

    front.to_csv(OUT / "ESM26_1m.csv", index=False)
    back.to_csv(OUT / "ESU26_1m.csv", index=False)

    # invalid sample: duplicate ts, high<low, negative volume, out-of-order
    bad = pd.DataFrame({
        "datetime": ["2026-06-08 09:30:00", "2026-06-08 09:30:00",
                     "2026-06-08 09:29:00", "2026-06-08 09:32:00"],
        "open": [5000, 5001, 5002, 5003],
        "high": [5005, 4990, 5006, 5007],   # row2 high<low
        "low":  [4998, 4999, 5000, 5001],
        "close":[5002, 5000, 5003, 5004],
        "volume":[100, -50, 100, 100],      # row2 negative volume
    })
    bad.to_csv(OUT / "BAD_1m.csv", index=False)

    print(f"wrote sample CSVs to {OUT}")
    for p in sorted(OUT.glob("*.csv")):
        print(f"  {p.name}: {sum(1 for _ in open(p)) - 1} rows")


if __name__ == "__main__":
    main()
