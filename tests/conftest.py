"""Shared test fixtures and synthetic data builders (deterministic)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.base import MarketDataProvider


class FakeProvider(MarketDataProvider):
    """In-memory provider returning pre-built raw OHLCV frames."""
    source_timestamp_convention = "open"

    def __init__(self, mapping: dict[tuple[str, str, str], pd.DataFrame]):
        # key: (symbol, timeframe, contract)
        self.mapping = mapping

    def get_raw_bars(self, symbol, timeframe, contract=None):
        return self.mapping[(symbol, timeframe, contract)].copy()

    def list_contracts(self, symbol):
        return sorted({k[2] for k in self.mapping if k[0] == symbol})


def make_rth_1m(dates: list[date], start_price=5000.0, seed=0,
                day_volume=390.0) -> pd.DataFrame:
    """Build OPEN-labeled naive-ET 1m RTH bars (09:30..15:59) for given dates.
    Deterministic; valid OHLC (high>=max(o,c), low<=min(o,c))."""
    rng = np.random.default_rng(seed)
    rows = []
    price = start_price
    per_bar_vol = day_volume / 390.0
    for d in dates:
        for i in range(390):  # 09:30 .. 15:59 open times
            ts = datetime.combine(d, time(9, 30)) + timedelta(minutes=i)
            o = price
            c = o + float(rng.normal(0, 1.0))
            h = max(o, c) + abs(float(rng.normal(0, 0.3)))
            l = min(o, c) - abs(float(rng.normal(0, 0.3)))
            rows.append((ts, o, h, l, c, per_bar_vol))
            price = c
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
    return df.set_index("datetime")


@pytest.fixture
def config() -> EngineConfig:
    return EngineConfig.load()


@pytest.fixture
def registry() -> ContractRegistry:
    return ContractRegistry.load()


@pytest.fixture
def engine(config, registry) -> DataEngine:
    return DataEngine(config, registry)


TZ = "America/New_York"


def make_bars_from_ohlc(rows, start="2026-06-15 09:31", tz=TZ, volume=100.0):
    """Build a CLOSE-labeled OHLCV frame from explicit (open,high,low,close) rows."""
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz=tz)
    idx.name = "timestamp"
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = volume
    return df


def make_bars_from_closes(closes, spread=0.5, **kw):
    """Build bars from a close path; high/low bracket each bar by `spread`."""
    rows = []
    prev = closes[0]
    for c in closes:
        o = prev
        h = max(o, c) + spread
        l = min(o, c) - spread
        rows.append((o, h, l, c))
        prev = c
    return make_bars_from_ohlc(rows, **kw)


def tz_bars(times, ohlc, session="RTH", tdate=date(2026, 6, 15), volume=100.0):
    """Build annotated OHLCV bars at explicit tz-aware timestamps (for setup tests)."""
    from signal_engine.data.models import SESSION, TRADING_DATE
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz=TZ) for t in times], name="timestamp")
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = volume
    df[SESSION] = session
    df[TRADING_DATE] = tdate
    return df


def make_ctx(bars, tf="5m", symbol="ES", session="RTH"):
    """Minimal AnalysisContext with all three roles pointing at `bars`."""
    from signal_engine.data.context import AnalysisContext, TimeframeView
    v = TimeframeView(tf, bars)
    return AnalysisContext(symbol, bars.index[-1], session, v, v, v)


def make_analysis(tf, ts, indicators=None, swings=None, structure=None,
                  fvg=None, liquidity=None, obs=None):
    from signal_engine.features.analyzer import MarketAnalysis, TimeframeAnalysis
    ta = TimeframeAnalysis(tf, ts, indicators if indicators is not None else pd.DataFrame(),
                           swings or [], structure or [], fvg or [], liquidity or [], obs or [])
    return MarketAnalysis("ES", ts, {tf: ta})


def weekdays(start: date, n: int) -> list[date]:
    """n consecutive weekdays starting at/after `start`."""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
