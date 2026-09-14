from datetime import date

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_series_equal

from signal_engine.features import indicators as ind
from tests.conftest import FakeProvider, make_rth_1m

TZ = "America/New_York"


@pytest.fixture
def rth_bars(engine):
    raw = make_rth_1m([date(2026, 6, 15)], seed=3)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    return lc.bars


def test_ema_matches_manual():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ind.ema(s, 2)
    # adjust=False EMA with span=2 -> alpha=2/3
    a = 2 / 3
    exp = s.iloc[0]
    vals = [exp]
    for x in s.iloc[1:]:
        exp = a * x + (1 - a) * exp
        vals.append(exp)
    # min_periods=2 -> first is NaN
    assert np.isnan(out.iloc[0])
    assert out.iloc[-1] == pytest.approx(vals[-1])


def test_rsi_bounds(rth_bars):
    r = ind.rsi(rth_bars["close"], 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_atr_positive(rth_bars):
    a = ind.atr(rth_bars, 14).dropna()
    assert (a > 0).all()


def test_vwap_session_reset():
    from signal_engine.data.models import SESSION, TRADING_DATE
    # two sessions same day: ETH then RTH
    idx = pd.date_range("2026-06-15 09:31", periods=6, freq="1min", tz=TZ)
    df = pd.DataFrame({
        "open": 10.0, "high": [11, 12, 13, 21, 22, 23], "low": [9, 10, 11, 19, 20, 21],
        "close": [10, 11, 12, 20, 21, 22], "volume": 100.0,
    }, index=idx)
    df[SESSION] = ["ETH", "ETH", "ETH", "RTH", "RTH", "RTH"]
    df[TRADING_DATE] = date(2026, 6, 15)
    v = ind.vwap(df)
    # first bar of each session: vwap == that bar's typical price
    tp0 = (11 + 9 + 10) / 3
    tp3 = (21 + 19 + 20) / 3
    assert v.iloc[0] == pytest.approx(tp0)
    assert v.iloc[3] == pytest.approx(tp3)   # reset at RTH open


def test_relative_volume():
    v = pd.Series([10.0] * 19 + [30.0])
    rv = ind.relative_volume(v, 20)
    # last bar: 30 / mean(19*10 + 30)/20 = 30 / 11 = 2.727..
    assert rv.iloc[-1] == pytest.approx(30 / ((19 * 10 + 30) / 20))


@pytest.mark.parametrize("fn_name", ["ema", "rsi", "atr", "vwap", "relative_volume"])
def test_indicator_no_lookahead(rth_bars, fn_name):
    """Causal: value at bar i is unchanged when future bars are appended."""
    N = 120
    full = rth_bars
    trunc = rth_bars.iloc[:N]

    def compute(df):
        if fn_name == "ema":
            return ind.ema(df["close"], 20)
        if fn_name == "rsi":
            return ind.rsi(df["close"], 14)
        if fn_name == "atr":
            return ind.atr(df, 14)
        if fn_name == "vwap":
            return ind.vwap(df)
        return ind.relative_volume(df["volume"], 20)

    a = compute(full).iloc[:N]
    b = compute(trunc)
    assert_series_equal(a, b, check_freq=False)
