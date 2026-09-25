"""Bar replay for manual practice (dashboard "Práctica").

Serves a window of historical 1-minute bars around any day, resampled to the
timeframe the operator picks. The downloaded CSV can hold 16 years (~5.5M
rows), so the first request converts it once to a Parquet file with small
row groups; after that a day's window is read with a time filter in well
under a second instead of parsing 300 MB of text.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pandas as pd

TIMEFRAMES = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
              "30m": "30min", "1h": "1h"}
_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NY = "America/New_York"


class ReplayError(ValueError):
    pass


class ReplayData:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _parquet(self, symbol: str, data: str) -> Path:
        if symbol not in ("NQ", "ES") or not _ID.match(data or ""):
            raise ReplayError("datos no válidos")
        csv = self.root / "market_data" / symbol / f"{data}_1m.csv"
        pq = csv.with_suffix(".parquet")
        if not csv.exists():
            raise ReplayError(f"no hay datos {data} de {symbol}")
        with self._lock:
            if not pq.exists() or pq.stat().st_mtime < csv.stat().st_mtime:
                df = pd.read_csv(csv, index_col="datetime", parse_dates=True)
                df = df[~df.index.duplicated(keep="last")].sort_index()
                tmp = pq.with_suffix(".parquet.tmp")
                df.to_parquet(tmp, row_group_size=50_000)
                tmp.replace(pq)
        return pq

    def bars(self, *, symbol: str, data: str, date: str, tf: str,
             days_before: int = 3, days_after: int = 2, at: str = "09:30") -> dict:
        """Bars from `days_before` days before `date` to `days_after` after,
        at timeframe `tf`, stamped by their OPEN time in New York (what a
        chart shows). The client reveals them one at a time."""
        if tf not in TIMEFRAMES:
            raise ReplayError("temporalidad no válida")
        if not _DATE.match(date or ""):
            raise ReplayError("fecha no válida")
        days_before = max(0, min(int(days_before), 10))
        days_after = max(1, min(int(days_after), 10))
        day = pd.Timestamp(date)
        lo = day - pd.Timedelta(days=days_before)
        hi = day + pd.Timedelta(days=days_after + 1)
        df = pd.read_parquet(self._parquet(symbol, data),
                             filters=[("datetime", ">=", lo), ("datetime", "<", hi)])
        if not re.match(r"^\d{2}:\d{2}$", at or ""):
            raise ReplayError("hora no válida")
        at_t = int(pd.Timestamp(f"{date} {at}").tz_localize(_NY).timestamp())
        if df.empty:
            return {"symbol": symbol, "tf": tf, "bars": [], "at_t": at_t}
        if tf != "1m":
            df = df.resample(TIMEFRAMES[tf], label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last",
                 "volume": "sum"}).dropna(subset=["open"])
        idx = df.index.tz_localize(_NY, ambiguous="NaT", nonexistent="shift_forward")
        keep = ~idx.isna()
        df, idx = df[keep], idx[keep]
        epoch = (idx.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) \
            // pd.Timedelta(seconds=1)
        bars = [{"t": int(t), "o": float(o), "h": float(h), "l": float(l_),
                 "c": float(c), "v": float(v)}
                for t, o, h, l_, c, v in zip(epoch, df["open"], df["high"], df["low"],
                                             df["close"], df["volume"])]
        return {"symbol": symbol, "tf": tf, "bars": bars, "at_t": at_t}
