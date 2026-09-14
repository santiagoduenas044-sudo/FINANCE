from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import LiquiditySweepParams
from signal_engine.data.contracts import ContractRegistry
from signal_engine.features.market_structure.models import Direction, LiquidityLevel
from signal_engine.setups.liquidity_sweep import LiquiditySweepDetector
from tests.conftest import make_analysis, make_ctx, tz_bars

TZ = "America/New_York"


@pytest.fixture
def det():
    return LiquiditySweepDetector(LiquiditySweepParams(), ContractRegistry.load())


def _sell_side_setup():
    lvl = LiquidityLevel(pd.Timestamp("2026-06-15 09:40", tz=TZ), 100.0, Direction.BEARISH, None)
    bars = tz_bars(["2026-06-15 09:45", "2026-06-15 09:50"],
                   [(101, 102, 100.5, 101.5), (101.5, 101.8, 98.0, 101.0)])  # N sweeps 100, closes 101
    an = make_analysis("5m", bars.index[-1], liquidity=[lvl])
    return bars, an, lvl


def test_detects_bullish_sweep(det):
    bars, an, lvl = _sell_side_setup()
    cands = det.detect(make_ctx(bars), an)
    assert len(cands) == 1
    c = cands[0]
    assert c.direction is Direction.BULLISH
    assert c.entry == 101.0
    assert c.stop_loss == pytest.approx(98.0 - 2 * 0.25)      # sweep low minus buffer
    assert c.timestamp == bars.index[-1]
    # entry bar is at/after the swept level's formation time (never before info)
    assert c.timestamp > lvl.timestamp
    # stop is NOT a future low: equals bar N's low - buffer
    assert c.stop_loss < c.entry


def test_no_sweep_when_close_not_reclaimed(det):
    lvl = LiquidityLevel(pd.Timestamp("2026-06-15 09:40", tz=TZ), 100.0, Direction.BEARISH, None)
    # low pierces 100 but close stays below (no reclaim) -> no candidate
    bars = tz_bars(["2026-06-15 09:45", "2026-06-15 09:50"],
                   [(101, 102, 100.5, 101), (101, 101, 98, 99)])
    an = make_analysis("5m", bars.index[-1], liquidity=[lvl])
    assert det.detect(make_ctx(bars), an) == []


def test_level_must_pre_exist_bar_n(det):
    # level formed AT bar N -> not usable
    N = pd.Timestamp("2026-06-15 09:50", tz=TZ)
    lvl = LiquidityLevel(N, 100.0, Direction.BEARISH, None)
    bars = tz_bars(["2026-06-15 09:45", "2026-06-15 09:50"],
                   [(101, 102, 100.5, 101.5), (101.5, 101.8, 98.0, 101.0)])
    an = make_analysis("5m", N, liquidity=[lvl])
    assert det.detect(make_ctx(bars), an) == []


def test_buy_side_sweep_is_bearish(det):
    lvl = LiquidityLevel(pd.Timestamp("2026-06-15 09:40", tz=TZ), 105.0, Direction.BULLISH, None)
    bars = tz_bars(["2026-06-15 09:45", "2026-06-15 09:50"],
                   [(104, 104.5, 103, 104), (104, 107, 103.5, 104.5)])  # high 107>105, close 104.5<105
    an = make_analysis("5m", bars.index[-1], liquidity=[lvl])
    cands = det.detect(make_ctx(bars), an)
    assert len(cands) == 1 and cands[0].direction is Direction.BEARISH
    assert cands[0].stop_loss == pytest.approx(107 + 2 * 0.25)


def test_no_lookahead_future_bars_dont_change_candidate(det):
    """Appending bars after N (then re-slicing to N) yields the same candidate."""
    bars, an, lvl = _sell_side_setup()
    c1 = det.detect(make_ctx(bars), an)[0]
    # append future bars, then slice back to N and rebuild analysis identically
    extended = pd.concat([bars, tz_bars(["2026-06-15 09:55", "2026-06-15 10:00"],
                                        [(101, 105, 100, 104), (104, 106, 103, 105)])])
    sliced = extended[extended.index <= bars.index[-1]]
    an2 = make_analysis("5m", sliced.index[-1], liquidity=[lvl])
    c2 = det.detect(make_ctx(sliced), an2)[0]
    assert (c1.direction, c1.entry, c1.stop_loss, c1.take_profits) == \
           (c2.direction, c2.entry, c2.stop_loss, c2.take_profits)
