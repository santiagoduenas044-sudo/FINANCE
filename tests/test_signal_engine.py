"""Tests for the Signal Engine (Phase 6): SetupCandidate -> Signal | NoTrade.

No internet, no real network calls, no Discord. Signal Engine is tested purely
against SetupCandidate + AnalysisContext.

NOTE: the `engine` fixture (conftest.py) is a DataEngine. The Signal Engine
under test here is built locally as `sig_engine` to avoid shadowing it.
"""
from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import SignalConfig
from signal_engine.data.context import AnalysisContext, TimeframeView
from signal_engine.data.contracts import ContractRegistry
from signal_engine.features.market_structure.models import Direction
from signal_engine.setups.engine import SetupEngine
from signal_engine.setups.models import LevelRef, SetupCandidate, SetupStatus
from signal_engine.signals.engine import SignalEngine
from signal_engine.signals.models import Signal, SignalDirection, compute_signal_id
from signal_engine.data.models import SESSION, TRADING_DATE
from tests.conftest import FakeProvider, make_rth_1m, weekdays

TZ = "America/New_York"


def _bars_at(timestamps: list[pd.Timestamp], ohlc: list[tuple[float, float, float, float]],
            volume: float = 100.0, session: str = "RTH") -> pd.DataFrame:
    """Build an annotated OHLCV frame from already tz-aware timestamps (unlike
    tests.conftest.tz_bars, which expects naive strings to localize)."""
    idx = pd.DatetimeIndex(timestamps, name="timestamp")
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = volume
    df[SESSION] = session
    df[TRADING_DATE] = idx[0].date()
    return df


def _candidate(**overrides) -> SetupCandidate:
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    base = dict(
        setup_name="orb", instrument="ES", direction=Direction.BULLISH,
        timestamp=ts, timeframe="5m", entry=101.0, stop_loss=99.0,
        take_profits=[103.0, 105.0], risk_reward=[1.0, 2.0],
        reasons=["Opening range fully formed", "Closed above OR high"],
        invalidation_conditions=["Close back inside the opening range"],
        invalidation_price=99.0,
        valid_until=ts + pd.Timedelta(minutes=60),
        market_context={"session": "RTH", "or_high": 100.5, "or_low": 99.0},
        referenced=[LevelRef("opening_range_high", None, 100.5)],
        status=SetupStatus.VALID,
    )
    base.update(overrides)
    return SetupCandidate(**base)


def _ctx_for(candidate: SetupCandidate, entry_override: float | None = None,
            decision_time: pd.Timestamp | None = None) -> AnalysisContext:
    """Build a minimal AnalysisContext whose execution/structure/htf views all
    contain exactly one bar at candidate.timestamp, closing at the candidate's
    entry (so validate_entry_matches_context passes by default)."""
    close = candidate.entry if entry_override is None else entry_override
    bars = _bars_at([candidate.timestamp], [(close - 0.5, close + 0.5, close - 1.0, close)])
    v = TimeframeView(candidate.timeframe, bars)
    dt = decision_time if decision_time is not None else candidate.timestamp
    return AnalysisContext("ES", dt, "RTH", v, v, v)


@pytest.fixture
def sig_engine():
    return SignalEngine(SignalConfig())


# --------------------------- LONG / SHORT valid --------------------------- #
def test_long_valid(sig_engine):
    c = _candidate(direction=Direction.BULLISH, entry=101.0, stop_loss=99.0,
                   take_profits=[103.0, 105.0], risk_reward=[1.0, 2.0])
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert isinstance(result, Signal)
    assert result.direction is SignalDirection.LONG
    assert result.instrument == "ES"
    assert result.setup == "orb"
    assert result.timestamp == c.timestamp
    assert result.timeframe == "5m"
    assert result.entry == 101.0
    assert result.stop == 99.0
    assert result.take_profits == [103.0, 105.0]
    assert result.risk_reward == [1.0, 2.0]
    assert result.reasons == c.reasons
    assert result.market_context == c.market_context
    assert result.invalidation_conditions == c.invalidation_conditions
    assert result.invalidation_price == 99.0
    assert result.expiration == c.valid_until
    assert result.signal_id                      # non-empty deterministic id


def test_short_valid(sig_engine):
    c = _candidate(direction=Direction.BEARISH, entry=99.0, stop_loss=101.0,
                   take_profits=[97.0, 95.0], risk_reward=[1.0, 2.0])
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert isinstance(result, Signal)
    assert result.direction is SignalDirection.SHORT
    assert result.entry == 99.0
    assert result.stop == 101.0
    assert result.take_profits == [97.0, 95.0]


# ------------------------------- SL/TP invalid ----------------------------- #
def test_invalid_stop_side_for_long_is_no_trade(sig_engine):
    # stop ABOVE entry for a bullish/LONG candidate -> invalid
    c = _candidate(direction=Direction.BULLISH, entry=101.0, stop_loss=102.0,
                   take_profits=[105.0], risk_reward=[1.0])
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert any("stop" in r.lower() for r in result.reasons)
    assert result.candidate_direction is SignalDirection.LONG


def test_invalid_take_profit_side_for_short_is_no_trade(sig_engine):
    # take profit ABOVE entry for a bearish/SHORT candidate -> invalid
    c = _candidate(direction=Direction.BEARISH, entry=99.0, stop_loss=101.0,
                   take_profits=[100.0], risk_reward=[1.0])
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert any("take-profit" in r.lower() for r in result.reasons)


def test_zero_risk_distance_is_no_trade(sig_engine):
    c = _candidate(entry=100.0, stop_loss=100.0, take_profits=[105.0], risk_reward=[0.0])
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)


# -------------------------------- R:R invalid ------------------------------ #
def test_risk_reward_below_threshold_is_no_trade():
    cfg = SignalConfig(min_risk_reward=1.5)
    eng = SignalEngine(cfg)
    c = _candidate(direction=Direction.BULLISH, entry=101.0, stop_loss=99.0,
                   take_profits=[102.0], risk_reward=[0.5])   # RR = 1/2 = 0.5 < 1.5
    ctx = _ctx_for(c)
    result = eng.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert any("r:r" in r.lower() for r in result.reasons)


def test_risk_reward_at_or_above_threshold_passes():
    cfg = SignalConfig(min_risk_reward=1.0)
    eng = SignalEngine(cfg)
    c = _candidate(direction=Direction.BULLISH, entry=101.0, stop_loss=99.0,
                   take_profits=[103.0], risk_reward=[1.0])   # RR exactly 1.0
    ctx = _ctx_for(c)
    result = eng.evaluate(ctx, c)
    assert isinstance(result, Signal)


# -------------------------------- expiration -------------------------------- #
def test_expired_candidate_is_no_trade(sig_engine):
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    c = _candidate(timestamp=ts, valid_until=ts + pd.Timedelta(minutes=5))
    later = ts + pd.Timedelta(minutes=10)     # past valid_until
    ctx = _ctx_for(c, decision_time=later)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert any("expired" in r.lower() for r in result.reasons)


def test_not_yet_expired_candidate_can_be_a_signal(sig_engine):
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    c = _candidate(timestamp=ts, valid_until=ts + pd.Timedelta(minutes=30))
    slightly_later = ts + pd.Timedelta(minutes=5)
    ctx = _ctx_for(c, decision_time=slightly_later)
    result = sig_engine.evaluate(ctx, c)
    assert isinstance(result, Signal)


def test_invalid_or_expired_status_is_rejected(sig_engine):
    c = _candidate(status=SetupStatus.EXPIRED)
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert any("expired" in r.lower() or "status" in r.lower() for r in result.reasons)

    c2 = _candidate(status=SetupStatus.INVALID)
    ctx2 = _ctx_for(c2)
    result2 = sig_engine.evaluate(ctx2, c2)
    assert not isinstance(result2, Signal)


# ---------------------------------- conflicts -------------------------------- #
def test_conflicting_setups_produce_no_trade(sig_engine):
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    bullish = _candidate(setup_name="orb", direction=Direction.BULLISH,
                         timestamp=ts, entry=101.0, stop_loss=99.0,
                         take_profits=[103.0], risk_reward=[1.0])
    bearish = _candidate(setup_name="vwap_reclaim", direction=Direction.BEARISH,
                         timestamp=ts, entry=101.0, stop_loss=103.0,
                         take_profits=[99.0], risk_reward=[1.0])
    ctx = _ctx_for(bullish)
    results = sig_engine.evaluate_candidates(ctx, [bullish, bearish])
    assert len(results) == 2
    assert all(not isinstance(r, Signal) for r in results)
    for r in results:
        assert any("conflict" in reason.lower() for reason in r.reasons)


def test_agreeing_setups_are_not_a_conflict(sig_engine):
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    c1 = _candidate(setup_name="orb", direction=Direction.BULLISH, timestamp=ts,
                    entry=101.0, stop_loss=99.0, take_profits=[103.0], risk_reward=[1.0])
    c2 = _candidate(setup_name="liquidity_sweep", direction=Direction.BULLISH, timestamp=ts,
                    entry=101.0, stop_loss=99.5, take_profits=[104.0], risk_reward=[2.0])
    ctx = _ctx_for(c1)
    results = sig_engine.evaluate_candidates(ctx, [c1, c2])
    assert len(results) == 2
    assert all(isinstance(r, Signal) for r in results)


def test_conflict_only_within_same_instrument():
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    es_long = _candidate(instrument="ES", direction=Direction.BULLISH, timestamp=ts,
                         entry=101.0, stop_loss=99.0, take_profits=[103.0], risk_reward=[1.0])
    nq_short = _candidate(instrument="NQ", direction=Direction.BEARISH, timestamp=ts,
                          entry=101.0, stop_loss=103.0, take_profits=[99.0], risk_reward=[1.0])
    eng = SignalEngine(SignalConfig())
    ctx = _ctx_for(es_long)
    results = eng.evaluate_candidates(ctx, [es_long, nq_short])
    assert all(isinstance(r, Signal) for r in results)   # different instruments, no conflict


# ---------------------------------- duplicates -------------------------------- #
def test_duplicate_candidate_is_not_reprocessed():
    c = _candidate()
    ctx = _ctx_for(c)
    eng = SignalEngine(SignalConfig(deduplication_enabled=True))
    first = eng.evaluate_candidates(ctx, [c])
    second = eng.evaluate_candidates(ctx, [c])   # identical candidate again
    assert len(first) == 1
    assert len(second) == 0                       # deduped, nothing re-emitted


def test_deduplication_disabled_reprocesses():
    c = _candidate()
    ctx = _ctx_for(c)
    eng = SignalEngine(SignalConfig(deduplication_enabled=False))
    first = eng.evaluate_candidates(ctx, [c])
    second = eng.evaluate_candidates(ctx, [c])
    assert len(first) == 1 and len(second) == 1


# -------------------------------- deterministic ID ----------------------------- #
def test_signal_id_is_deterministic_and_no_randomness():
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    id1 = compute_signal_id("ES", "orb", SignalDirection.LONG, ts, "5m", 101.0, 99.0)
    id2 = compute_signal_id("ES", "orb", SignalDirection.LONG, ts, "5m", 101.0, 99.0)
    assert id1 == id2


def test_signal_id_differs_by_relevant_field():
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    base = ("ES", "orb", SignalDirection.LONG, ts, "5m", 101.0, 99.0)
    variants = [
        ("NQ", "orb", SignalDirection.LONG, ts, "5m", 101.0, 99.0),
        ("ES", "vwap_reclaim", SignalDirection.LONG, ts, "5m", 101.0, 99.0),
        ("ES", "orb", SignalDirection.SHORT, ts, "5m", 101.0, 99.0),
        ("ES", "orb", SignalDirection.LONG, ts + pd.Timedelta(minutes=5), "5m", 101.0, 99.0),
        ("ES", "orb", SignalDirection.LONG, ts, "15m", 101.0, 99.0),
        ("ES", "orb", SignalDirection.LONG, ts, "5m", 101.5, 99.0),
        ("ES", "orb", SignalDirection.LONG, ts, "5m", 101.0, 98.5),
    ]
    base_id = compute_signal_id(*base)
    for v in variants:
        assert compute_signal_id(*v) != base_id


def test_evaluate_produces_same_signal_id_across_fresh_engines():
    c = _candidate()
    ctx = _ctx_for(c)
    r1 = SignalEngine(SignalConfig()).evaluate(ctx, c)
    r2 = SignalEngine(SignalConfig()).evaluate(ctx, c)
    assert isinstance(r1, Signal) and isinstance(r2, Signal)
    assert r1.signal_id == r2.signal_id


# ---------------------------------- NO_TRADE ------------------------------------ #
def test_no_trade_always_has_reasons(sig_engine):
    c = _candidate(stop_loss=102.0)   # invalid side for BULLISH
    ctx = _ctx_for(c)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert len(result.reasons) >= 1
    assert result.instrument == c.instrument
    assert result.timestamp == c.timestamp
    assert result.setup == c.setup_name


def test_no_trade_accumulates_multiple_reasons(sig_engine):
    """Several rules can fail at once; NoTrade should explain all of them, not
    just the first."""
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    c = _candidate(
        timestamp=ts, entry=101.0, stop_loss=102.0,   # bad side (LONG, stop above entry)
        take_profits=[100.5], risk_reward=[0.1],      # also bad TP side + would-be-bad RR
        valid_until=ts - pd.Timedelta(minutes=1),     # already expired
    )
    ctx = _ctx_for(c, decision_time=ts)
    result = sig_engine.evaluate(ctx, c)
    assert not isinstance(result, Signal)
    assert len(result.reasons) >= 2


# --------------------------------- no-lookahead --------------------------------- #
def test_entry_validation_ignores_bars_after_candidate_timestamp(sig_engine):
    """Even if the AnalysisContext's view happens to contain rows AFTER the
    candidate's own timestamp, validation must key off the candidate's own bar
    only — never be swayed by what comes later."""
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    c = _candidate(timestamp=ts, entry=101.0, stop_loss=99.0,
                  take_profits=[103.0], risk_reward=[1.0],
                  valid_until=ts + pd.Timedelta(minutes=30))

    only_n = _bars_at([ts], [(100.5, 101.5, 100.0, 101.0)])
    v1 = TimeframeView("5m", only_n)
    ctx1 = AnalysisContext("ES", ts, "RTH", v1, v1, v1)
    r1 = sig_engine.evaluate(ctx1, c)

    future_rows = _bars_at([ts + pd.Timedelta(minutes=5), ts + pd.Timedelta(minutes=10)],
                          [(101, 200, 101, 199), (199, 199, 1, 2)])
    with_future = pd.concat([only_n, future_rows])
    v2 = TimeframeView("5m", with_future)
    # decision_time still == ts: a well-formed AnalysisContext would never
    # include these future rows, but we defensively prove the validator would
    # not use them even if they were present.
    ctx2 = AnalysisContext("ES", ts, "RTH", v2, v2, v2)
    r2 = sig_engine.evaluate(ctx2, c)

    assert isinstance(r1, Signal) and isinstance(r2, Signal)
    assert r1.signal_id == r2.signal_id
    assert r1.entry == r2.entry == 101.0


def _setup_pipeline(data_engine):
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 2), seed=7)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = data_engine.load_contract(provider, "ES", "ESM26", "1m")
    data_engine.build_timeframes(lc)
    return lc


def test_no_lookahead_end_to_end(engine):
    """Full pipeline: SetupEngine.detect -> SignalEngine.evaluate_candidates.
    For every decision time with results on the full dataset, re-running the
    SAME pipeline with data truncated to that N (fresh engines each time, so
    dedup state never leaks across N) yields IDENTICAL Signal/NoTrade output.
    This proves neither engine ever uses information beyond the decision bar,
    and that the same engine logic works identically for backtest replay and
    for a live/paper "as-of now" evaluation.
    """
    setup_engine = SetupEngine(engine.cfg, ContractRegistry.load())
    lc = _setup_pipeline(engine)
    dts = list(lc.timeframes["5m"].index[8:])

    def outcome_key(results):
        out = []
        for r in results:
            if isinstance(r, Signal):
                out.append(("SIGNAL", r.setup, r.direction.value, round(r.entry, 4),
                           round(r.stop, 4), tuple(round(t, 4) for t in r.take_profits)))
            else:
                out.append(("NO_TRADE", r.setup, tuple(r.reasons)))
        return out

    checked = 0
    for N in dts:
        ctx_full = engine.analysis_context("ES", N, lc.timeframes)
        candidates_full = setup_engine.detect(ctx_full)
        if not candidates_full:
            continue

        result_full = SignalEngine(SignalConfig()).evaluate_candidates(ctx_full, candidates_full)

        tf_trunc = {tf: df[df.index <= N] for tf, df in lc.timeframes.items()}
        ctx_trunc = engine.analysis_context("ES", N, tf_trunc)
        candidates_trunc = setup_engine.detect(ctx_trunc)
        result_trunc = SignalEngine(SignalConfig()).evaluate_candidates(ctx_trunc, candidates_trunc)

        assert outcome_key(result_full) == outcome_key(result_trunc)
        checked += 1

    assert checked > 0


def test_signal_timestamp_never_after_decision_time(engine):
    """Every produced Signal's timestamp must be <= the decision time it was
    evaluated at (never a future timestamp)."""
    setup_engine = SetupEngine(engine.cfg, ContractRegistry.load())
    sig = SignalEngine(SignalConfig())
    lc = _setup_pipeline(engine)
    checked = 0
    for N in lc.timeframes["5m"].index[8:]:
        ctx = engine.analysis_context("ES", N, lc.timeframes)
        candidates = setup_engine.detect(ctx)
        for result in sig.evaluate_candidates(ctx, candidates):
            assert result.timestamp <= N
            checked += 1
        if checked >= 20:
            break
    assert checked > 0
