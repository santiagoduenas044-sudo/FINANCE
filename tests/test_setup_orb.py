from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig, OrbParams
from signal_engine.data.sessions import SessionClassifier
from signal_engine.features.market_structure.models import Direction
from signal_engine.setups.orb import OrbDetector
from tests.conftest import make_analysis, make_ctx, tz_bars

TZ = "America/New_York"

OR_TIMES = ["2026-06-15 09:35", "2026-06-15 09:40", "2026-06-15 09:45",
            "2026-06-15 09:50", "2026-06-15 09:55", "2026-06-15 10:00"]
OR_OHLC = [(100, 102, 99, 101), (101, 103, 100, 102), (102, 105, 101, 104),
           (104, 104, 101, 103), (103, 104, 102, 103), (103, 105, 102, 104)]
# OR window [09:30,10:00] -> high 105, low 99, range 6


@pytest.fixture
def det():
    cfg = EngineConfig.load()
    return OrbDetector(OrbParams(opening_range_minutes=30),
                       SessionClassifier(cfg.sessions), "America/New_York")


def _bars_with_breakout():
    times = OR_TIMES + ["2026-06-15 10:05"]
    ohlc = OR_OHLC + [(104, 107, 104, 106)]     # closes 106 > OR high 105
    return tz_bars(times, ohlc)


def _analysis(bars):
    return make_analysis("5m", bars.index[-1])


def test_breakout_after_or_complete(det):
    bars = _bars_with_breakout()
    cands = det.detect(make_ctx(bars), _analysis(bars))
    assert len(cands) == 1
    c = cands[0]
    assert c.direction is Direction.BULLISH
    assert c.entry == 106.0
    assert c.stop_loss == 99.0                       # OR low
    assert c.take_profits == [112.0, 118.0]          # entry + [1,2]*range(6)
    assert c.market_context["or_high"] == 105.0 and c.market_context["or_low"] == 99.0


def test_no_breakout_before_or_formed(det):
    """Within the OR window there is NO breakout, even if price exceeds a partial
    range."""
    # only first 5 OR bars (up to 09:55); OR window not complete
    bars = tz_bars(OR_TIMES[:5], OR_OHLC[:5])
    assert det.detect(make_ctx(bars), _analysis(bars)) == []


def test_no_breakout_exactly_at_or_end(det):
    # N == or_end (10:00): OR just formed, no post-OR bar yet
    bars = tz_bars(OR_TIMES, OR_OHLC)
    assert det.detect(make_ctx(bars), _analysis(bars)) == []


def test_or_levels_stable_when_future_added(det):
    """OR high/low computed at breakout do not change when future bars appended
    (they never depend on future bars)."""
    bars = _bars_with_breakout()
    c1 = det.detect(make_ctx(bars), _analysis(bars))[0]
    extended = pd.concat([bars, tz_bars(["2026-06-15 10:10", "2026-06-15 10:15"],
                                        [(106, 130, 106, 129), (129, 130, 90, 95)])])
    sliced = extended[extended.index <= bars.index[-1]]
    c2 = det.detect(make_ctx(sliced), _analysis(sliced))[0]
    assert (c1.entry, c1.stop_loss, c1.take_profits,
            c1.market_context["or_high"], c1.market_context["or_low"]) == \
           (c2.entry, c2.stop_loss, c2.take_profits,
            c2.market_context["or_high"], c2.market_context["or_low"])


def test_bearish_breakdown(det):
    times = OR_TIMES + ["2026-06-15 10:05"]
    ohlc = OR_OHLC + [(102, 102, 97, 98)]      # closes 98 < OR low 99
    bars = tz_bars(times, ohlc)
    cands = det.detect(make_ctx(bars), _analysis(bars))
    assert len(cands) == 1 and cands[0].direction is Direction.BEARISH
    assert cands[0].stop_loss == 105.0         # OR high
