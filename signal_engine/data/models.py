"""Canonical bar schema constants and timeframe helpers.

Canonical normalized DataFrame contract used across the Data Engine:
  - index: tz-aware DatetimeIndex named ``timestamp`` marking each bar's CLOSE
  - columns: open, high, low, close, volume (float64)
  - sorted ascending, unique index
After annotation, two more columns may be present: ``session`` and
``trading_date``.
"""
from __future__ import annotations

import pandas as pd

TIMESTAMP = "timestamp"
OPEN, HIGH, LOW, CLOSE, VOLUME = "open", "high", "low", "close", "volume"
OHLCV = [OPEN, HIGH, LOW, CLOSE, VOLUME]
OHLC = [OPEN, HIGH, LOW, CLOSE]

SESSION = "session"
TRADING_DATE = "trading_date"

# session labels
RTH = "RTH"
ETH = "ETH"
CLOSED = "CLOSED"

# timeframe string -> pandas offset alias
_TF_OFFSET = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1H": "1h",
    "2H": "2h",
    "4H": "4h",
}

# timeframe string -> pandas Timedelta
_TF_DELTA = {tf: pd.Timedelta(off) for tf, off in _TF_OFFSET.items()}


def tf_to_offset(tf: str) -> str:
    if tf not in _TF_OFFSET:
        raise ValueError(f"unsupported timeframe: {tf!r}")
    return _TF_OFFSET[tf]


def tf_to_timedelta(tf: str) -> pd.Timedelta:
    if tf not in _TF_DELTA:
        raise ValueError(f"unsupported timeframe: {tf!r}")
    return _TF_DELTA[tf]


def tf_rank(tf: str) -> pd.Timedelta:
    """Ordering key so we can assert target >= base timeframe."""
    return tf_to_timedelta(tf)


def empty_ohlcv(tz: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz=tz, name=TIMESTAMP)
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in OHLCV}, index=idx)
