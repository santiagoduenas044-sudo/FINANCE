"""Technical indicators. Every function is CAUSAL: the value at bar i depends
only on bars <= i, so it is no-lookahead by construction and stable under
truncation. Inputs are as-of OHLCV frames (the AnalysisContext views); outputs
are pandas Series/columns aligned to the input index.

No strategy logic here — these are raw analysis tools.
"""
from __future__ import annotations

import pandas as pd

from ..data.models import (
    CLOSE, HIGH, LOW, OPEN, SESSION, TRADING_DATE, VOLUME,
)


def ema(close: pd.Series, period: int = 20) -> pd.Series:
    """Exponential moving average (adjust=False => causal recursion)."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (ewm alpha=1/period). Causal."""
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    roll_down = down.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = roll_up / roll_down
    out = 100.0 - 100.0 / (1.0 + rs)
    # when roll_down == 0 -> RSI 100; when both 0 -> NaN handled by pandas
    out = out.where(roll_down != 0, 100.0)
    out[roll_up.isna()] = float("nan")
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df[CLOSE].shift(1)
    hl = df[HIGH] - df[LOW]
    hc = (df[HIGH] - prev_close).abs()
    lc = (df[LOW] - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR. Causal (uses shift(1) close only)."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _session_group_key(df: pd.DataFrame) -> pd.Series:
    """Group key that resets per (trading_date, session) if annotated, else a
    single anchor for the whole frame."""
    if SESSION in df.columns and TRADING_DATE in df.columns:
        return list(zip(df[TRADING_DATE], df[SESSION]))
    return [0] * len(df)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP. Cumulative within each (trading_date, session)
    group => resets at session open; causal cumulative sum."""
    tp = (df[HIGH] + df[LOW] + df[CLOSE]) / 3.0
    key = _session_group_key(df)
    grouper = pd.Series(key, index=df.index)
    pv = (tp * df[VOLUME]).groupby(grouper, sort=False).cumsum()
    cv = df[VOLUME].groupby(grouper, sort=False).cumsum()
    out = pv / cv
    out.name = "vwap"
    return out


def relative_volume(volume: pd.Series, window: int = 20) -> pd.Series:
    """Rolling relative volume: volume / mean(volume over last `window` bars).
    Causal (rolling window ends at the current bar).

    NOTE: this is a rolling-baseline RVOL, not a same-time-of-day RVOL (see
    docs limitations)."""
    baseline = volume.rolling(window, min_periods=window).mean()
    return volume / baseline


def compute_indicators(
    df: pd.DataFrame,
    ema_periods: tuple[int, ...] = (20, 50),
    rsi_period: int = 14,
    atr_period: int = 14,
    rvol_window: int = 20,
) -> pd.DataFrame:
    """Convenience: compute the standard indicator set as columns.
    Returns a new DataFrame indexed like `df` (does not mutate input)."""
    out = pd.DataFrame(index=df.index)
    for p in ema_periods:
        out[f"ema_{p}"] = ema(df[CLOSE], p)
    out[f"rsi_{rsi_period}"] = rsi(df[CLOSE], rsi_period)
    out[f"atr_{atr_period}"] = atr(df, atr_period)
    out["vwap"] = vwap(df)
    out[f"rvol_{rvol_window}"] = relative_volume(df[VOLUME], rvol_window)
    return out
