"""Tests for the smart-money-concepts adapter: it must return our domain models
(not smc types), drop trailing unconfirmed detections, and be no-lookahead."""
from datetime import date

import pandas as pd
import pytest

from signal_engine.data.resample import resample_ohlcv
from signal_engine.features.market_structure.models import (
    FairValueGap, LiquidityLevel, OrderBlock, StructureEvent, SwingPoint,
)
from tests.conftest import FakeProvider, make_rth_1m, weekdays

TZ = "America/New_York"
smc_adapter = pytest.importorskip("smartmoneyconcepts")  # skip if lib missing
from signal_engine.features.market_structure.smc_adapter import SmcStructureProvider  # noqa: E402


@pytest.fixture
def bars(engine):
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 3), seed=4)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    return resample_ohlcv(lc.bars, "5m", engine.cfg.sessions, engine.cfg.resample, TZ)


@pytest.fixture
def p():
    return SmcStructureProvider(swing_length=5)


def test_returns_domain_models(bars, p):
    assert all(isinstance(s, SwingPoint) for s in p.swings(bars))
    assert all(isinstance(e, StructureEvent) for e in p.structure_events(bars))
    assert all(isinstance(f, FairValueGap) for f in p.fair_value_gaps(bars))
    assert all(isinstance(x, LiquidityLevel) for x in p.liquidity_levels(bars))
    assert all(isinstance(o, OrderBlock) for o in p.order_blocks(bars))


def test_drops_trailing_unconfirmed_swings(bars, p):
    """No swing may fall inside the trailing `swing_length` bars (unconfirmed)."""
    sw = p.swings(bars)
    assert sw, "expected some confirmed swings"
    cutoff_ts = bars.index[len(bars) - p.swing_length - 1]
    assert all(s.timestamp <= cutoff_ts for s in sw)


def _core(o, exclude=()):
    return tuple(v for k, v in o.__dict__.items() if k not in exclude)


def _assert_stable(method, bars, pN, pM, exclude=()):
    ev_N = method(bars.iloc[:pN])
    ev_M = method(bars.iloc[:pM])
    setM = {_core(e, exclude) for e in ev_M}
    assert ev_N, "expected events in truncated window"
    for e in ev_N:
        assert _core(e, exclude) in setM, f"smc event changed when future added: {e}"


def test_no_lookahead_swings(bars, p):
    _assert_stable(p.swings, bars, 120, 240)


def test_no_lookahead_structure(bars, p):
    ev = p.structure_events(bars.iloc[:120])
    if ev:
        _assert_stable(p.structure_events, bars, 120, 240)


def test_no_lookahead_fvg(bars, p):
    _assert_stable(p.fair_value_gaps, bars, 120, 240, exclude=("mitigated_at",))
