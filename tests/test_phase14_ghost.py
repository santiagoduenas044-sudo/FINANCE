"""Phase 14 — GHOST intelligence tests: market regime, multi-timeframe context,
structure, liquidity, key levels, confluence (explainable + configurable),
NO_TRADE guards, no-lookahead, and the runtime/API integration.
"""
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.config.schema import EngineConfig, GhostConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.data.ingestion import normalize
from signal_engine.features.analyzer import TimeframeAnalysis
from signal_engine.features.market_structure.models import Direction, StructureEvent, StructureType
from signal_engine.ghost.brain import GhostBrain
from signal_engine.ghost.confluence import score_confluence
from signal_engine.ghost.models import (
    Bias, KeyLevel, LiquidityState, MarketRegime, MarketState, MTFAlignment,
    MTFContext, NoTradeReason, RegimeState, StructureState, TimeframeBias, Volatility,
)
from signal_engine.ghost.regime import classify_regime
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from signal_engine.signals.models import Signal, SignalDirection

SAMPLES = Path(__file__).resolve().parent / "data_samples"
GCFG = GhostConfig()


# --------------------------------------------------------------------------- #
# Regime classifier (synthetic, deterministic)
# --------------------------------------------------------------------------- #
def _ta(ema_fast, ema_slow, atr, events=None):
    idx = pd.date_range("2026-06-15 09:30", periods=len(ema_fast), freq="5min",
                        tz="America/New_York")
    ind = pd.DataFrame({"ema_20": ema_fast, "ema_50": ema_slow, "atr_14": atr,
                        "rvol_20": [1.0] * len(ema_fast), "vwap": ema_slow}, index=idx)
    return TimeframeAnalysis("5m", idx[-1], ind, [], events or [], [], [])


def test_regime_unknown_when_insufficient_data():
    ta = _ta([100] * 5, [100] * 5, [1] * 5)
    assert classify_regime(ta, GCFG, ta.decision_time).regime is MarketRegime.UNKNOWN


def test_regime_trend_up():
    n = 40
    fast = [99 + i * 0.1 for i in range(n)]     # rising, ends ~103
    slow = [100] * n
    r = classify_regime(_ta(fast, slow, [1.0] * n), GCFG, _ta(fast, slow, [1.0] * n).decision_time)
    assert r.regime is MarketRegime.TREND_UP and r.trend_strength > 0


def test_regime_trend_down():
    n = 40
    fast = [101 - i * 0.1 for i in range(n)]
    slow = [100] * n
    r = classify_regime(_ta(fast, slow, [1.0] * n), GCFG, _ta(fast, slow, [1.0] * n).decision_time)
    assert r.regime is MarketRegime.TREND_DOWN and r.trend_strength < 0


def test_regime_range_normal_vol():
    n = 40
    fast = [100.1] * n
    slow = [100.0] * n
    atr = list(range(1, n)) + [n // 2]          # last is mid -> NORMAL vol
    r = classify_regime(_ta(fast, slow, atr), GCFG, _ta(fast, slow, atr).decision_time)
    assert r.regime is MarketRegime.RANGE and r.volatility is Volatility.NORMAL


def test_regime_high_volatility():
    n = 40
    atr = [float(i) for i in range(1, n + 1)]   # last is max -> HIGH
    r = classify_regime(_ta([100.1] * n, [100.0] * n, atr), GCFG,
                        _ta([100.1] * n, [100.0] * n, atr).decision_time)
    assert r.regime is MarketRegime.HIGH_VOLATILITY and r.volatility is Volatility.HIGH


def test_regime_low_volatility():
    n = 40
    atr = [float(i) for i in range(n, 0, -1)]   # last is min -> LOW
    r = classify_regime(_ta([100.1] * n, [100.0] * n, atr), GCFG,
                        _ta([100.1] * n, [100.0] * n, atr).decision_time)
    assert r.regime is MarketRegime.LOW_VOLATILITY and r.volatility is Volatility.LOW


def test_regime_transition_on_recent_choch():
    n = 40
    fast = [99 + i * 0.1 for i in range(n)]     # would be TREND_UP...
    slow = [100] * n
    ta = _ta(fast, slow, [1.0] * n)
    ev = StructureEvent(timestamp=ta.indicators.index[-2], type=StructureType.CHOCH,
                        direction=Direction.BEARISH, level=100.0,
                        swing_timestamp=ta.indicators.index[-5])
    ta2 = _ta(fast, slow, [1.0] * n, events=[ev])
    assert classify_regime(ta2, GCFG, ta2.decision_time).regime is MarketRegime.TRANSITION


# --------------------------------------------------------------------------- #
# Confluence traceability
# --------------------------------------------------------------------------- #
def _mk_state(*, regime=MarketRegime.RANGE, htf=Bias.NEUTRAL, struct=Bias.NEUTRAL,
              exec_=Bias.NEUTRAL, alignment=MTFAlignment.MIXED, session="RTH",
              vwap_side="ABOVE", atr_pct=0.5, sweep=None, reclaimed=False,
              changed=False) -> MarketState:
    reg = RegimeState(regime, Volatility.NORMAL, 0.0, atr_pct, 100.0, 100.0, 100.0, "", None)
    mtf = MTFContext(TimeframeBias("1H", htf), TimeframeBias("15m", struct),
                     TimeframeBias("5m", exec_), alignment)
    st = StructureState(struct, changed, "BOS", struct if struct is not Bias.NEUTRAL else None, "")
    liq = LiquidityState(sweep, 5000.0 if sweep else None, None, reclaimed, None, None, "")
    return MarketState("ES", pd.Timestamp("2026-06-15 10:00", tz="America/New_York"),
                       session, 100.0, 100.0, vwap_side, reg, mtf, st, liq, [])


def _mk_signal(direction="LONG", rr=(2.0,), setup="vwap_reclaim"):
    d = SignalDirection.LONG if direction == "LONG" else SignalDirection.SHORT
    return Signal(signal_id="s", instrument="ES", setup=setup, direction=d,
                  timestamp=pd.Timestamp("2026-06-15 10:00", tz="America/New_York"),
                  timeframe="5m", entry=5000, stop=4995, take_profits=[5010],
                  risk_reward=list(rr), reasons=[], invalidation_price=4995)


def test_confluence_score_is_sum_of_contributions():
    ms = _mk_state(htf=Bias.BULLISH, struct=Bias.BULLISH, exec_=Bias.BULLISH,
                   alignment=MTFAlignment.ALIGNED, regime=MarketRegime.TREND_UP,
                   sweep=Bias.BEARISH, reclaimed=True)
    res = score_confluence("LONG", ms, _mk_signal(rr=(2.0,)), None, GCFG)
    assert res.score == pytest.approx(round(sum(c.points for c in res.contributions), 2))
    assert res.score > 0 and res.direction == "LONG"
    # explains WHY: itemised, named contributions
    names = {c.name for c in res.contributions}
    assert "htf_alignment" in names and "regime_alignment" in names


def test_confluence_conflict_lowers_score():
    aligned = score_confluence("LONG", _mk_state(htf=Bias.BULLISH, struct=Bias.BULLISH,
                               regime=MarketRegime.TREND_UP), _mk_signal(), None, GCFG)
    conflict = score_confluence("LONG", _mk_state(htf=Bias.BEARISH, struct=Bias.BEARISH,
                                regime=MarketRegime.TREND_DOWN), _mk_signal(), None, GCFG)
    assert conflict.score < aligned.score


def test_weights_are_configurable():
    cfg = GhostConfig()
    cfg.weights.htf_alignment = 100.0
    ms = _mk_state(htf=Bias.BULLISH)
    res = score_confluence("LONG", ms, _mk_signal(), None, cfg)
    assert any(c.name == "htf_alignment" and c.points == 100.0 for c in res.contributions)


# --------------------------------------------------------------------------- #
# NO_TRADE guards (brain)
# --------------------------------------------------------------------------- #
class _FakeCtx:
    def __init__(self, ms):
        class V:  # minimal execution view
            timeframe = "5m"
        self.execution = V()


def _decide(ms, sig, cfg=None, **kw):
    brain = GhostBrain(cfg or GhostConfig())
    return brain._decide_signal(sig, ms, None, None, kw.get("data_stale", False),
                                kw.get("conflicting", False), kw.get("has_active"))


def test_no_trade_conflicting_timeframe():
    d = _decide(_mk_state(htf=Bias.BEARISH), _mk_signal("LONG"))
    assert d.action == "NO_TRADE" and NoTradeReason.CONFLICTING_TIMEFRAME in d.no_trade_reasons


def test_no_trade_against_structure():
    d = _decide(_mk_state(struct=Bias.BEARISH), _mk_signal("LONG"))
    assert d.action == "NO_TRADE" and NoTradeReason.AGAINST_STRUCTURE in d.no_trade_reasons


def test_no_trade_excessive_volatility():
    d = _decide(_mk_state(atr_pct=0.99), _mk_signal("LONG"))
    assert NoTradeReason.EXCESSIVE_VOLATILITY in d.no_trade_reasons


def test_no_trade_invalid_session():
    d = _decide(_mk_state(session="ASIA"), _mk_signal("LONG"))
    assert NoTradeReason.INVALID_SESSION in d.no_trade_reasons


def test_no_trade_target_too_close():
    d = _decide(_mk_state(), _mk_signal("LONG", rr=(0.5,)))
    assert NoTradeReason.TARGET_TOO_CLOSE in d.no_trade_reasons


def test_no_trade_stale_and_duplicate():
    d1 = _decide(_mk_state(), _mk_signal("LONG"), data_stale=True)
    assert NoTradeReason.STALE_DATA in d1.no_trade_reasons
    # Phase 15: per-symbol semantic — an open trade blocks with ACTIVE_TRADE
    d2 = _decide(_mk_state(), _mk_signal("LONG"), has_active=lambda *a: True)
    assert NoTradeReason.ACTIVE_TRADE in d2.no_trade_reasons


def test_no_trade_insufficient_confluence():
    cfg = GhostConfig()
    cfg.min_confluence_score = 999.0
    d = _decide(_mk_state(htf=Bias.BULLISH, struct=Bias.BULLISH, exec_=Bias.BULLISH,
                          alignment=MTFAlignment.ALIGNED), _mk_signal("LONG"), cfg=cfg)
    assert d.action == "NO_TRADE"
    assert d.no_trade_reasons == [NoTradeReason.INSUFFICIENT_CONFLUENCE]
    assert d.confluence is not None                      # score is still attached/visible


def test_approved_trade_when_aligned():
    cfg = GhostConfig()
    cfg.min_confluence_score = 10.0
    d = _decide(_mk_state(htf=Bias.BULLISH, struct=Bias.BULLISH, exec_=Bias.BULLISH,
                          alignment=MTFAlignment.ALIGNED, regime=MarketRegime.TREND_UP,
                          sweep=Bias.BEARISH, reclaimed=True), _mk_signal("LONG"), cfg=cfg)
    assert d.action == "LONG" and d.is_trade and d.confluence.passed


# --------------------------------------------------------------------------- #
# Integration on real sample data
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def pipeline():
    de = DataEngine(); reg = ContractRegistry.load()
    lc = de.load_contract(CsvProvider(SAMPLES), "ES", "ESM26", "1m")
    tf = de.build_timeframes(lc)
    se = SetupEngine(de.cfg, reg); sig = SignalEngine(de.cfg.signals)
    brain = GhostBrain(de.cfg.ghost)
    return de, se, sig, brain, tf


def test_market_state_is_rich_and_structured(pipeline):
    de, se, sig, brain, tf = pipeline
    exec_tf = de.cfg.timeframe_roles.execution
    N = tf[exec_tf].index[120]
    ctx = de.analysis_context("ES", N, tf)
    ms = brain.market_state(ctx, se.analyzer.analyze(ctx))
    assert ms.regime.regime in list(MarketRegime)
    assert ms.mtf.alignment in list(MTFAlignment)
    assert ms.structure.bias in list(Bias)
    assert ms.vwap_side in ("ABOVE", "BELOW", "AT", "UNKNOWN")
    assert isinstance(ms.key_levels, list) and len(ms.key_levels) > 0
    d = ms.to_dict()                                    # JSON-safe
    import json
    json.loads(json.dumps(d))


def test_ghost_produces_trades_and_no_trades_over_session(pipeline):
    de, se, sig, brain, tf = pipeline
    exec_tf = de.cfg.timeframe_roles.execution
    trades = no_trades = 0
    for N in tf[exec_tf].index[60::3]:
        ctx = de.analysis_context("ES", N, tf)
        analysis = se.analyzer.analyze(ctx)
        base = sig.evaluate_candidates(ctx, se.detect(ctx))
        _, decisions = brain.evaluate(ctx, analysis, base)
        for d in decisions:
            if d.is_trade:
                trades += 1
            else:
                no_trades += 1
                assert d.no_trade_reasons  # every NO_TRADE is explained
    assert no_trades > 0            # GHOST rejects setups (setup != trade)


def test_no_lookahead_decision_is_asof_only(pipeline):
    """A decision at bar N is identical whether or not future bars exist in the
    frame — GHOST only ever sees data up to the decision bar."""
    de, se, sig, brain, tf = pipeline
    exec_tf = de.cfg.timeframe_roles.execution
    idx = tf[exec_tf].index
    N = idx[150]
    full = de.analysis_context("ES", N, tf)
    truncated_tf = {k: v[v.index <= N] for k, v in tf.items()}
    trunc = de.analysis_context("ES", N, truncated_tf)
    ms_full = brain.market_state(full, se.analyzer.analyze(full)).to_dict()
    ms_trunc = brain.market_state(trunc, se.analyzer.analyze(trunc)).to_dict()
    assert ms_full == ms_trunc


def test_runtime_ghost_and_paper_integration():
    de = DataEngine(); reg = ContractRegistry.load()
    hist = normalize(CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26"),
                     de.cfg.data, "1m")
    feed = LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=5), "ES", de.cfg)
    tracker = PaperTradeTracker()
    # GHOST now trades RTH only by default (valid_sessions=["RTH"]), kept as-is
    # here deliberately — this smoke test should catch a break in that guard
    # too. But the fixture opens Sunday 18:00 ET, so RTH (Monday 09:30) does
    # not start until cycle ~144 of 200, leaving only ~70 RTH minutes visible
    # — too few for the real min_confluence_score=35 to clear reliably, and
    # running enough cycles to traverse the whole ~4140-minute fixture takes
    # minutes (indicator recompute cost grows with accumulated history). This
    # is a WIRING smoke test, not a threshold calibration, so lower the score
    # bar the same way the Phase 15 authority tests do; the session guard
    # itself is untouched.
    gcfg = GhostConfig(min_confluence_score=0.0, block_htf_conflict=False,
                       block_against_structure=False, min_stop_atr_mult=0.0)
    rt = BotRuntime(de, SetupEngine(de.cfg, reg), SignalEngine(de.cfg.signals), reg,
                    live_feeds={"ES": feed}, ghost=GhostBrain(gcfg),
                    paper_tracker=tracker)
    rt.start()
    for _ in range(200):
        rt.run_cycle("ES")
    # GHOST populated market state; paper trades were opened and tracked.
    assert "ES" in rt.ghost_market_state
    total = len(tracker.open_trades) + len(tracker.closed_trades)
    assert total >= 1

    client = TestClient(create_app(rt))
    assert client.get("/api/ghost/market/ES").json()["enabled"] is True
    assert client.get("/api/ghost/stats").json()["enabled"] is True
    hist_resp = client.get("/api/ghost/trades/history").json()
    assert hist_resp["enabled"] is True


def test_runtime_without_ghost_is_unchanged():
    de = DataEngine(); reg = ContractRegistry.load()
    hist = normalize(CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26"),
                     de.cfg.data, "1m")
    feed = LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=100000), "ES", de.cfg)
    feed.poll()
    rt = BotRuntime(de, SetupEngine(de.cfg, reg), SignalEngine(de.cfg.signals), reg,
                    live_feeds={"ES": feed})   # no ghost, no paper
    rt.start(); rt.run_cycle("ES")
    status = rt.to_status_dict()
    assert status["ghost"]["enabled"] is False
    assert status["ghost"]["active_trades"] == 0
    client = TestClient(create_app(rt))
    assert client.get("/api/ghost/trades/active").json()["enabled"] is False
