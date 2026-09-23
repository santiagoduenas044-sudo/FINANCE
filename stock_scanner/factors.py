"""Per-ticker raw factors, computed as of the LAST bar of the frame passed in.

Raw factors are not comparable across factors; `scanner.py` turns them into
cross-sectional percentiles before weighting. Because every value only uses
bars up to the last one, the backtest gets point-in-time factors simply by
truncating history.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class Factors:
    ticker: str
    date: pd.Timestamp
    close: float
    bars: int
    avg_dollar_volume: float
    sma50: float
    sma200: float
    ema20: float
    atr14: float
    rsi14: float
    macd_hist: float
    high_52w: float
    low_52w: float
    ret_1m: float
    ret_3m: float
    ret_6m: float
    mom_12_1: float
    rel_strength_6m: float
    vol_60d: float
    volume_ratio: float       # 20d avg volume / 60d avg volume
    accumulation: float       # up-day volume / down-day volume, last 50 bars
    trend: float              # 0..1, absolute
    timing: float             # 0..1, absolute
    ext_atr: float            # (close - EMA20) / ATR

    @property
    def high_proximity(self) -> float:
        return self.close / self.high_52w if self.high_52w > 0 else float("nan")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["high_proximity"] = self.high_proximity
        return d


def _last(s: pd.Series) -> float:
    v = s.iloc[-1] if len(s) else float("nan")
    return float(v) if pd.notna(v) else float("nan")


def trend_state(close: float, sma50: float, sma200: float, sma50_prev: float) -> float:
    """Fraction of four classic uptrend conditions that hold."""
    checks = [close > sma50, close > sma200, sma50 > sma200, sma50 > sma50_prev]
    known = [c for c, v in zip(checks, (sma50, sma200, sma200, sma50_prev)) if not math.isnan(v)]
    return float(np.mean(known)) if known else float("nan")


def timing_score(ext_atr: float, rsi: float, overbought: float = 75.0) -> float:
    """1.0 = price close to its EMA20 with RSI in a healthy 40-65 band (a low
    risk place to enter); decays as the stock gets stretched or breaks down."""
    if math.isnan(ext_atr) or math.isnan(rsi):
        return float("nan")
    # -0.5..+1.5 ATR from EMA20 is the sweet spot; >3.5 ATR above is chasing.
    if -0.5 <= ext_atr <= 1.5:
        stretch = 1.0
    elif ext_atr > 1.5:
        stretch = max(0.0, 1.0 - (ext_atr - 1.5) / 2.0)
    else:
        stretch = max(0.0, 1.0 - (-0.5 - ext_atr) / 2.0)
    if 40 <= rsi <= 65:
        osc = 1.0
    elif rsi > 65:
        osc = max(0.0, 1.0 - (rsi - 65) / (overbought + 10 - 65))
    else:
        osc = max(0.0, 1.0 - (40 - rsi) / 20)
    return 0.6 * stretch + 0.4 * osc


def compute_factors(ticker: str, df: pd.DataFrame, benchmark_close: pd.Series | None = None,
                    rsi_overbought: float = 75.0) -> Factors:
    close = df["close"]
    volume = df["volume"]
    sma50_s = ind.sma(close, 50)
    sma200 = _last(ind.sma(close, 200))
    sma50 = _last(sma50_s)
    sma50_prev = float(sma50_s.iloc[-21]) if len(sma50_s) > 21 and pd.notna(sma50_s.iloc[-21]) else float("nan")
    ema20 = _last(ind.ema(close, 20))
    atr14 = _last(ind.atr(df, 14))
    rsi14 = _last(ind.rsi(close, 14))
    last = float(close.iloc[-1])
    ext = (last - ema20) / atr14 if atr14 and not math.isnan(atr14) else float("nan")

    window = df.iloc[-252:]
    ret_6m = ind.pct_return(close, 126)
    rel = float("nan")
    if benchmark_close is not None:
        bench = benchmark_close.loc[:df.index[-1]]
        rel = ret_6m - ind.pct_return(bench, 126)

    tail = df.iloc[-51:]
    chg = tail["close"].diff().iloc[1:]
    vol_tail = tail["volume"].iloc[1:]
    down_vol = float(vol_tail[chg < 0].sum())
    accumulation = float(vol_tail[chg > 0].sum()) / down_vol if down_vol > 0 else float("nan")

    avg_vol_60 = float(volume.iloc[-60:].mean())
    return Factors(
        ticker=ticker,
        date=df.index[-1],
        close=last,
        bars=len(df),
        avg_dollar_volume=float((close.iloc[-20:] * volume.iloc[-20:]).mean()),
        sma50=sma50,
        sma200=sma200,
        ema20=ema20,
        atr14=atr14,
        rsi14=rsi14,
        macd_hist=_last(ind.macd_hist(close)),
        high_52w=float(window["high"].max()),
        low_52w=float(window["low"].min()),
        ret_1m=ind.pct_return(close, 21),
        ret_3m=ind.pct_return(close, 63),
        ret_6m=ret_6m,
        mom_12_1=ind.pct_return(close, 252, skip=21),
        rel_strength_6m=rel,
        vol_60d=ind.realized_vol(close, 60),
        volume_ratio=float(volume.iloc[-20:].mean()) / avg_vol_60 if avg_vol_60 > 0 else float("nan"),
        accumulation=accumulation,
        trend=trend_state(last, sma50, sma200, sma50_prev),
        timing=timing_score(ext, rsi14, rsi_overbought),
        ext_atr=ext,
    )
