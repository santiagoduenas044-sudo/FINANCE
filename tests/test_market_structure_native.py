from datetime import date

import pandas as pd
import pytest

from signal_engine.data.resample import resample_ohlcv
from signal_engine.features.market_structure.models import (
    Direction, StructureType, SwingLabel, SwingType,
)
from signal_engine.features.market_structure.native import NativeStructureProvider
from tests.conftest import FakeProvider, make_bars_from_ohlc, make_rth_1m, weekdays

TZ = "America/New_York"

# a hand-built zig-zag with isolated extrema (verified behavior)
ZIGZAG = [(10, 11, 9, 10), (10, 15, 9, 14), (14, 13, 7, 8), (8, 17, 8, 16),
          (16, 15, 6, 7), (7, 19, 7, 4), (4, 17, 5, 6), (6, 20, 6, 19)]


@pytest.fixture
def p():
    return NativeStructureProvider(n_left=1, n_right=1)


@pytest.fixture
def struct_bars(engine):
    """Multi-day 5m RTH bars for no-lookahead stress tests."""
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 3), seed=9)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    return resample_ohlcv(lc.bars, "5m", engine.cfg.sessions, engine.cfg.resample, TZ)


def test_swings_and_labels(p):
    bars = make_bars_from_ohlc(ZIGZAG)
    sw = p.swings(bars)
    highs = [s for s in sw if s.type is SwingType.HIGH]
    lows = [s for s in sw if s.type is SwingType.LOW]
    assert [round(s.price) for s in highs] == [15, 17, 19]
    assert [round(s.price) for s in lows] == [7, 6, 5]
    # last bar (no right neighbor) is never a confirmed swing
    assert all(s.timestamp < bars.index[-1] for s in sw)
    # labels: rising highs => HH; falling lows => LL (first of each = None)
    assert highs[1].label is SwingLabel.HH and highs[2].label is SwingLabel.HH
    assert lows[1].label is SwingLabel.LL and lows[2].label is SwingLabel.LL


def test_bos_then_choch(p):
    bars = make_bars_from_ohlc(ZIGZAG)
    ev = p.structure_events(bars)
    kinds = [(e.type, e.direction) for e in ev]
    assert (StructureType.BOS, Direction.BULLISH) in kinds
    assert (StructureType.CHOCH, Direction.BEARISH) in kinds


def test_fvg_detection_and_mitigation(p):
    rows = [(9, 10, 8, 9.5), (9.5, 13, 9.5, 12.5), (12.5, 13.5, 11, 13),
            (13, 13.2, 10.5, 11)]  # bullish gap [10,11], mitigated by bar 4
    bars = make_bars_from_ohlc(rows)
    fvgs = p.fair_value_gaps(bars)
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.direction is Direction.BULLISH
    assert f.top == pytest.approx(11.0) and f.bottom == pytest.approx(10.0)
    assert f.mitigated_at == bars.index[3]


def test_liquidity_equal_highs_and_sweep():
    p = NativeStructureProvider(n_left=1, n_right=1, liquidity_tolerance=0.02)
    rows = [(10, 10.5, 9, 10), (10, 11.00, 9, 10.8), (10.8, 10.0, 9, 9.5),
            (9.5, 11.03, 9, 10.9), (10.9, 10.2, 9, 9.6), (9.6, 12.0, 9, 11.5)]
    bars = make_bars_from_ohlc(rows)
    liq = p.liquidity_levels(bars)
    assert len(liq) == 1
    lv = liq[0]
    assert lv.direction is Direction.BULLISH
    assert lv.level == pytest.approx(11.015, abs=0.02)
    assert lv.swept_at == bars.index[5]      # swept by the 12.0 high


def test_order_blocks_present(p):
    bars = make_bars_from_ohlc(ZIGZAG)
    obs = p.order_blocks(bars)
    assert len(obs) >= 1
    assert all(o.top >= o.bottom for o in obs)


# --------------------------- no-lookahead ------------------------------- #
def _core(o, exclude=()):
    return tuple(v for k, v in o.__dict__.items() if k not in exclude)


def _assert_stable(method, bars, pN, pM, exclude=()):
    ev_N = method(bars.iloc[:pN])
    ev_M = method(bars.iloc[:pM])
    setM = {_core(e, exclude) for e in ev_M}
    assert ev_N, "expected some events in the truncated window"
    for e in ev_N:
        assert _core(e, exclude) in setM, f"event changed when future added: {e}"


def test_no_lookahead_swings(struct_bars):
    p = NativeStructureProvider(n_left=2, n_right=2)
    _assert_stable(p.swings, struct_bars, 120, 240)


def test_no_lookahead_structure(struct_bars):
    p = NativeStructureProvider(n_left=2, n_right=2)
    _assert_stable(p.structure_events, struct_bars, 120, 240)


def test_no_lookahead_fvg(struct_bars):
    p = NativeStructureProvider(n_left=2, n_right=2)
    _assert_stable(p.fair_value_gaps, struct_bars, 120, 240, exclude=("mitigated_at",))


def test_no_lookahead_liquidity(struct_bars):
    p = NativeStructureProvider(n_left=2, n_right=2, liquidity_tolerance=0.01)
    # liquidity may be sparse; only assert stability if any exist
    ev = p.liquidity_levels(struct_bars.iloc[:120])
    if ev:
        _assert_stable(p.liquidity_levels, struct_bars, 120, 240, exclude=("swept_at",))


def test_fvg_mitigation_monotonic(struct_bars):
    """Once mitigated, an FVG stays mitigated (mitigation never un-happens)."""
    p = NativeStructureProvider(n_left=2, n_right=2)
    fN = {(_core(f, ("mitigated_at",))): f.mitigated_at
          for f in p.fair_value_gaps(struct_bars.iloc[:150])}
    for f in p.fair_value_gaps(struct_bars.iloc[:250]):
        key = _core(f, ("mitigated_at",))
        if key in fN and fN[key] is not None:
            assert f.mitigated_at == fN[key]
