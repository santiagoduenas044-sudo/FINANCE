from datetime import date

import pandas as pd

from signal_engine.data.models import RTH
from tests.conftest import FakeProvider, make_rth_1m

TZ = "America/New_York"
SYMBOL = "ES"
CONTRACT = "ESM26"


def _ctx(engine, N):
    raw = make_rth_1m([date(2026, 6, 15)], seed=5)
    provider = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    lc = engine.load_contract(provider, SYMBOL, CONTRACT, "1m")
    engine.build_timeframes(lc)
    return engine.analysis_context(SYMBOL, N, lc.timeframes), lc


def test_context_roles_and_asof(engine):
    N = pd.Timestamp("2026-06-15 11:07", tz=TZ)
    ctx, lc = _ctx(engine, N)
    assert ctx.symbol == SYMBOL
    assert ctx.session == RTH
    # role wiring per config (context=1H, structure=15m, execution=5m)
    assert ctx.htf.timeframe == "1H"
    assert ctx.structure.timeframe == "15m"
    assert ctx.execution.timeframe == "5m"
    # as-of: no bar in any view closes after N
    for view in (ctx.htf, ctx.structure, ctx.execution):
        assert (view.bars.index <= N).all()
        assert not view.bars.empty


def test_context_last_closed_execution_bar(engine):
    N = pd.Timestamp("2026-06-15 11:07", tz=TZ)
    ctx, _ = _ctx(engine, N)
    # last visible 5m bar closes at 11:05 (forming 11:10 excluded)
    assert ctx.execution.bars.index[-1] == pd.Timestamp("2026-06-15 11:05", tz=TZ)


def test_context_naive_decision_time_localized(engine):
    N = pd.Timestamp("2026-06-15 11:07")  # naive
    ctx, _ = _ctx(engine, N)
    assert ctx.decision_time.tzinfo is not None
