"""Daily-bar indicators. Every function is CAUSAL (value at bar i uses only
bars <= i), so the backtest can reuse them on truncated history without
lookahead. Inputs are OHLCV frames with lowercase columns."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period, min_periods=period).mean()


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    up = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    down = (-delta).clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    out = 100.0 - 100.0 / (1.0 + up / down)
    return out.where(down != 0, 100.0).where(up.notna())


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    line = ema(close, fast) - ema(close, slow)
    return line - line.ewm(span=signal, adjust=False, min_periods=signal).mean()


def pct_return(close: pd.Series, lookback: int, skip: int = 0) -> float:
    """Return from `lookback` bars ago to `skip` bars ago (skip=21 gives the
    classic 12-1 momentum that ignores the last month's reversal)."""
    if len(close) <= lookback:
        return float("nan")
    end = close.iloc[-1 - skip]
    start = close.iloc[-1 - lookback]
    return float(end / start - 1.0) if start > 0 else float("nan")


def realized_vol(close: pd.Series, period: int = 60) -> float:
    """Annualized stdev of daily log returns over the last `period` bars."""
    rets = np.log(close).diff().dropna().iloc[-period:]
    if len(rets) < period // 2:
        return float("nan")
    return float(rets.std() * np.sqrt(252))


def max_drawdown(close: pd.Series) -> float:
    """Largest peak-to-trough decline (negative number, e.g. -0.23)."""
    if close.empty:
        return float("nan")
    return float((close / close.cummax() - 1.0).min())
