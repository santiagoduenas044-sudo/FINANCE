from datetime import date

import pandas as pd

from signal_engine.config.schema import EngineConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.setups.engine import SetupEngine
from tests.conftest import FakeProvider, make_rth_1m, weekdays


def _pipeline(engine):
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 2), seed=7)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    engine.build_timeframes(lc)
    return lc


def test_setups_toggle_individually(engine):
    engine.cfg.setups.orb.enabled = False
    se = SetupEngine(engine.cfg, ContractRegistry.load())
    assert "orb" not in se.enabled_setups
    assert "liquidity_sweep" in se.enabled_setups
    assert "vwap_reclaim" in se.enabled_setups


def test_setups_are_independent(engine):
    """Liquidity Sweep must work with VWAP Reclaim and ORB disabled."""
    engine.cfg.setups.vwap_reclaim.enabled = False
    engine.cfg.setups.orb.enabled = False
    se = SetupEngine(engine.cfg, ContractRegistry.load())
    assert se.enabled_setups == ["liquidity_sweep"]
    lc = _pipeline(engine)
    results = se.scan(engine, "ES", lc.timeframes, list(lc.timeframes["5m"].index[8:]))
    for cs in results.values():
        assert all(c.setup_name == "liquidity_sweep" for c in cs)


def test_all_three_fire_on_synthetic_data(engine):
    se = SetupEngine(engine.cfg, ContractRegistry.load())
    lc = _pipeline(engine)
    results = se.scan(engine, "ES", lc.timeframes, list(lc.timeframes["5m"].index[8:]))
    names = {c.setup_name for cs in results.values() for c in cs}
    assert {"liquidity_sweep", "vwap_reclaim", "orb"}.issubset(names)


def test_no_lookahead_end_to_end(engine):
    """For every decision time with candidates on the full dataset, re-detecting
    at that same N with data truncated to N yields identical candidates
    (proves detection never uses future bars)."""
    se = SetupEngine(engine.cfg, ContractRegistry.load())
    lc = _pipeline(engine)
    dts = list(lc.timeframes["5m"].index[8:])
    full = se.scan(engine, "ES", lc.timeframes, dts)

    def key(cs):
        return [(c.setup_name, c.direction.value, round(c.entry, 4),
                 round(c.stop_loss, 4), tuple(round(t, 4) for t in c.take_profits)) for c in cs]

    checked = 0
    for N, cands in full.items():
        tf_trunc = {tf: df[df.index <= N] for tf, df in lc.timeframes.items()}
        ctx = engine.analysis_context("ES", N, tf_trunc)
        assert key(cands) == key(se.detect(ctx))
        checked += 1
    assert checked > 0


def test_candidate_entry_timestamp_is_decision_bar(engine):
    se = SetupEngine(engine.cfg, ContractRegistry.load())
    lc = _pipeline(engine)
    results = se.scan(engine, "ES", lc.timeframes, list(lc.timeframes["5m"].index[8:]))
    for N, cs in results.items():
        for c in cs:
            assert c.timestamp == N
            assert c.valid_until > N
            for ref in c.referenced:
                if ref.timestamp is not None:
                    assert ref.timestamp <= N        # never references future info
