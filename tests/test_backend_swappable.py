"""The market-structure backend is swappable: both providers satisfy the same
interface and MarketAnalyzer works with either without any other change."""
from datetime import date

import pandas as pd
import pytest

from signal_engine.features.analyzer import MarketAnalysis, MarketAnalyzer
from signal_engine.features.market_structure import (
    MarketStructureProvider, get_structure_provider,
)
from tests.conftest import FakeProvider, make_rth_1m


def _context(engine):
    raw = make_rth_1m([date(2026, 6, 15)], seed=6)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    engine.build_timeframes(lc)
    N = pd.Timestamp("2026-06-15 14:30", tz="America/New_York")
    return engine.analysis_context("ES", N, lc.timeframes)


@pytest.mark.parametrize("backend", ["native", "smc"])
def test_backend_conforms_interface(backend):
    prov = get_structure_provider(backend, swing_length=5) if backend == "smc" \
        else get_structure_provider(backend, n_left=2, n_right=2)
    assert isinstance(prov, MarketStructureProvider)


@pytest.mark.parametrize("backend,kwargs", [
    ("native", {"n_left": 2, "n_right": 2}),
    ("smc", {"swing_length": 5}),
])
def test_analyzer_runs_with_each_backend(engine, backend, kwargs):
    ctx = _context(engine)
    analyzer = MarketAnalyzer(backend=backend, provider_kwargs=kwargs)
    result = analyzer.analyze(ctx)
    assert isinstance(result, MarketAnalysis)
    # all three roles analyzed, indicators present, everything as-of N
    for tf, ta in result.by_timeframe.items():
        assert not ta.indicators.empty
        assert (ta.indicators.index <= ctx.decision_time).all()
        for coll in (ta.swings, ta.structure_events, ta.fair_value_gaps,
                     ta.liquidity_levels, ta.order_blocks):
            for ev in coll:
                assert ev.timestamp <= ctx.decision_time
