from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import RolloverConfig
from signal_engine.data.models import CLOSE
from signal_engine.data.rollover import build_continuous, compute_roll_schedule
from tests.conftest import FakeProvider, make_rth_1m, weekdays

TZ = "America/New_York"
SYMBOL = "ES"


def _build_two_contracts(engine, front="ESM26", back="ESU26",
                         front_days=None, back_days=None,
                         back_factor=1.01):
    """Front contract heavy volume early, back contract overtakes on day 3."""
    days = weekdays(date(2026, 6, 8), 5)
    front_days = days
    back_days = days
    # front prices ~5000, back = front * back_factor (multiplicative offset)
    raw_front = make_rth_1m(front_days, start_price=5000.0, seed=1, day_volume=1000)
    raw_back = make_rth_1m(back_days, start_price=5000.0 * back_factor, seed=2, day_volume=1000)

    # Make back volume exceed front starting day index 3 (the 4th day)
    def scale_volume(raw, days_list, threshold_idx, low, high):
        raw = raw.copy()
        for i, d in enumerate(days_list):
            mask = (raw.index >= pd.Timestamp(d)) & (raw.index < pd.Timestamp(d) + pd.Timedelta(days=1))
            raw.loc[mask, "volume"] = (high if i >= threshold_idx else low) / 390.0
        return raw

    raw_front = scale_volume(raw_front, front_days, threshold_idx=3, low=2000, high=800)
    raw_back = scale_volume(raw_back, back_days, threshold_idx=3, low=500, high=3000)

    provider = FakeProvider({
        (SYMBOL, "1m", front): raw_front,
        (SYMBOL, "1m", back): raw_back,
    })
    dfs = {}
    for c in (front, back):
        lc = engine.load_contract(provider, SYMBOL, c, "1m")
        dfs[c] = lc.bars
    return dfs, days


def test_roll_schedule_reproducible(engine):
    dfs, days = _build_two_contracts(engine)
    cfg = RolloverConfig()
    ev1, w1 = compute_roll_schedule(dfs, cfg)
    ev2, w2 = compute_roll_schedule(dfs, cfg)
    assert ev1 == ev2
    assert w1 == w2
    assert len(ev1) == 1
    # roll happens on the 4th weekday (index 3), where back volume overtakes
    assert ev1[0].roll_date == days[3]
    assert ev1[0].from_contract == "ESM26"
    assert ev1[0].to_contract == "ESU26"


def test_continuous_reproducible_and_identical(engine):
    dfs, _ = _build_two_contracts(engine)
    cfg = RolloverConfig(continuous_adjustment="ratio")
    c1, _ = build_continuous(dfs, cfg)
    c2, _ = build_continuous(dfs, cfg)
    pd.testing.assert_frame_equal(c1, c2)


def test_ratio_series_removes_jump_raw_keeps_it(engine):
    dfs, days = _build_two_contracts(engine, back_factor=1.02)
    roll_date = days[3]

    raw_cont, ev = build_continuous(dfs, RolloverConfig(continuous_adjustment="raw"))
    ratio_cont, _ = build_continuous(dfs, RolloverConfig(continuous_adjustment="ratio"))

    def jump_at_roll(series):
        s = series.sort_index()
        before = s[s.index < pd.Timestamp(roll_date, tz=TZ)][CLOSE].iloc[-1]
        after = s[s.index >= pd.Timestamp(roll_date, tz=TZ)][CLOSE].iloc[0]
        return abs(after - before)

    raw_jump = jump_at_roll(raw_cont)
    ratio_jump = jump_at_roll(ratio_cont)
    # raw shows a big ~2% jump; ratio-adjusted junction is continuous
    assert raw_jump > 50            # ~2% of 5000
    assert ratio_jump < raw_jump    # ratio adjustment materially reduces the jump


def test_active_windows_cover_series(engine):
    dfs, days = _build_two_contracts(engine)
    _, windows = compute_roll_schedule(dfs, RolloverConfig())
    assert windows[0].contract_code == "ESM26"
    assert windows[-1].contract_code == "ESU26"
    assert windows[-1].end is None      # last contract open-ended
