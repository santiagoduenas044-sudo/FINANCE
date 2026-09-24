from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import VwapReclaimParams
from signal_engine.data.contracts import ContractRegistry
from signal_engine.features.market_structure.models import Direction, SwingPoint, SwingType
from signal_engine.setups.vwap_reclaim import VwapReclaimDetector
from tests.conftest import make_analysis, make_ctx, tz_bars

TZ = "America/New_York"


@pytest.fixture
def det():
    return VwapReclaimDetector(VwapReclaimParams(lookback_bars=12), ContractRegistry.load())


def _bull_reclaim():
    times = ["2026-06-15 09:40", "2026-06-15 09:45", "2026-06-15 09:50"]
    bars = tz_bars(times, [(100, 100.5, 98, 99), (99, 99.5, 98.5, 99), (99, 101.5, 98.8, 101)])
    vw = pd.Series([100.0, 100.0, 100.0], index=bars.index, name="vwap")
    sw = [SwingPoint(pd.Timestamp("2026-06-15 09:45", tz=TZ), 98.5, SwingType.LOW,
                     pd.Timestamp("2026-06-15 09:50", tz=TZ))]
    an = make_analysis("5m", bars.index[-1], indicators=pd.DataFrame({"vwap": vw}), swings=sw)
    return bars, an


def test_detects_bullish_reclaim(det):
    bars, an = _bull_reclaim()
    cands = det.detect(make_ctx(bars), an)
    assert len(cands) == 1
    c = cands[0]
    assert c.direction is Direction.BULLISH
    assert c.entry == 101.0
    # lowest low of the dip below VWAP (09:40 low 98.0) - buffer. The old
    # swing-based stop sat at 98.5, INSIDE the dip it was meant to protect.
    assert c.stop_loss == pytest.approx(98.0 - 2 * 0.25)
    assert c.timestamp == bars.index[-1]


def test_no_reclaim_when_already_above(det):
    times = ["2026-06-15 09:45", "2026-06-15 09:50"]
    bars = tz_bars(times, [(101, 102, 100.5, 101.5), (101.5, 102, 101, 101.8)])  # both above vwap
    vw = pd.Series([100.0, 100.0], index=bars.index, name="vwap")
    an = make_analysis("5m", bars.index[-1], indicators=pd.DataFrame({"vwap": vw}))
    assert det.detect(make_ctx(bars), an) == []


def test_bearish_loss(det):
    times = ["2026-06-15 09:40", "2026-06-15 09:45", "2026-06-15 09:50"]
    bars = tz_bars(times, [(100, 102, 100, 101), (101, 101.5, 100.5, 101), (101, 101.2, 98.5, 99)])
    vw = pd.Series([100.0, 100.0, 100.0], index=bars.index, name="vwap")
    an = make_analysis("5m", bars.index[-1], indicators=pd.DataFrame({"vwap": vw}))
    cands = det.detect(make_ctx(bars), an)
    assert len(cands) == 1 and cands[0].direction is Direction.BEARISH


def test_session_anchor_no_cross_session(det):
    """Prev bar in a different session must not create a reclaim."""
    from signal_engine.data.models import SESSION, TRADING_DATE
    times = ["2026-06-15 09:40", "2026-06-15 09:45"]
    bars = tz_bars(times, [(100, 100.5, 98, 99), (99, 101.5, 98.8, 101)])
    bars[SESSION] = ["ETH", "RTH"]      # session boundary between prev and N
    vw = pd.Series([100.0, 100.0], index=bars.index, name="vwap")
    an = make_analysis("5m", bars.index[-1], indicators=pd.DataFrame({"vwap": vw}))
    assert det.detect(make_ctx(bars), an) == []


def test_no_lookahead(det):
    bars, an = _bull_reclaim()
    c1 = det.detect(make_ctx(bars), an)[0]
    extended = pd.concat([bars, tz_bars(["2026-06-15 09:55"], [(101, 104, 100, 103)])])
    sliced = extended[extended.index <= bars.index[-1]]
    vw = pd.Series([100.0] * len(sliced), index=sliced.index, name="vwap")
    sw = an.by_timeframe["5m"].swings
    an2 = make_analysis("5m", sliced.index[-1], indicators=pd.DataFrame({"vwap": vw}), swings=sw)
    c2 = det.detect(make_ctx(sliced), an2)[0]
    assert (c1.entry, c1.stop_loss, c1.take_profits) == (c2.entry, c2.stop_loss, c2.take_profits)
