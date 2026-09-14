from datetime import date

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from signal_engine.data.models import CLOSE, HIGH, LOW, OHLC, OPEN, VOLUME
from tests.conftest import FakeProvider, make_rth_1m

TZ = "America/New_York"
SYMBOL = "ES"
CONTRACT = "ESM26"


def _load_base(engine, dates):
    raw = make_rth_1m(dates, seed=42)
    provider = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    lc = engine.load_contract(provider, SYMBOL, CONTRACT, "1m")
    engine.build_timeframes(lc)
    return lc


def test_resample_bins_are_close_labeled_and_valid(engine):
    lc = _load_base(engine, [date(2026, 6, 15)])
    for tf in ("5m", "15m", "1H"):
        df = lc.timeframes[tf]
        assert not df.empty
        # OHLC integrity preserved by aggregation
        assert (df[HIGH] >= df[LOW]).all()
        assert (df[HIGH] >= df[[OPEN, CLOSE]].max(axis=1)).all()
        assert (df[LOW] <= df[[OPEN, CLOSE]].min(axis=1)).all()
    # first 5m bar closes at 09:35 (covers 09:31..09:35 closes)
    assert lc.timeframes["5m"].index[0] == pd.Timestamp("2026-06-15 09:35", tz=TZ)
    # total RTH volume conserved across timeframes
    base_vol = lc.bars[VOLUME].sum()
    assert lc.timeframes["5m"][VOLUME].sum() == pytest.approx(base_vol)


def test_no_lookahead_truncation_equivalence(engine):
    """A resampled view up to N must equal resampling only data up to N,
    for every timeframe (as-of semantics)."""
    dates = [date(2026, 6, 15)]
    lc = _load_base(engine, dates)
    N = pd.Timestamp("2026-06-15 11:07", tz=TZ)

    # full-data context sliced to N
    ctx_full = engine.analysis_context(SYMBOL, N, lc.timeframes)

    # resample only base data up to N, then slice to N
    from signal_engine.data.resample import resample_ohlcv
    base_trunc = lc.bars[lc.bars.index <= N]
    tf_trunc = {"1m": base_trunc}
    for tf in ("5m", "15m", "1H"):
        tf_trunc[tf] = resample_ohlcv(
            base_trunc, tf, engine.cfg.sessions, engine.cfg.resample, TZ)
    ctx_trunc = engine.analysis_context(SYMBOL, N, tf_trunc)

    for role in ("htf", "structure", "execution"):
        a = getattr(ctx_full, role).bars[OHLC]
        b = getattr(ctx_trunc, role).bars[OHLC]
        assert_frame_equal(a, b, check_freq=False)


def test_no_duplicate_labels_or_empty_bins_with_eth(engine):
    """Regression: ETH empty bins (volume=0, NaN OHLC) must be dropped, so
    resampled frames never carry duplicate timestamps or NaN OHLC rows."""
    # raw 1m open-labeled ET: an ETH pre-market block + an RTH block, one day
    eth = pd.date_range("2026-06-15 08:00", "2026-06-15 09:29", freq="1min")
    rth = pd.date_range("2026-06-15 09:30", "2026-06-15 10:30", freq="1min")
    idx = eth.append(rth)
    raw = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5,
                        "close": 100.2, "volume": 10.0}, index=idx)
    raw.index.name = "raw_ts"
    provider = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    lc = engine.load_contract(provider, SYMBOL, CONTRACT, "1m")
    engine.build_timeframes(lc)
    for tf, df in lc.timeframes.items():
        assert not df.index.duplicated().any(), f"{tf} has duplicate timestamps"
        assert not df[OHLC].isna().any().any(), f"{tf} has NaN OHLC (empty bin)"


def test_future_bars_do_not_change_past(engine):
    """Appending future 1m bars must not alter any resampled bar with
    label <= N."""
    from signal_engine.data.resample import resample_ohlcv
    dates = [date(2026, 6, 15)]
    lc = _load_base(engine, dates)
    N = pd.Timestamp("2026-06-15 11:00", tz=TZ)

    full_1h = lc.timeframes["1H"]
    past_full = full_1h[full_1h.index <= N][OHLC]

    base_trunc = lc.bars[lc.bars.index <= N]
    trunc_1h = resample_ohlcv(base_trunc, "1H", engine.cfg.sessions,
                              engine.cfg.resample, TZ)
    past_trunc = trunc_1h[trunc_1h.index <= N][OHLC]

    assert_frame_equal(past_full, past_trunc, check_freq=False)
