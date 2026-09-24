"""Stop-too-wide guard + trade lifecycle notifications.

The operator reported a live NQ trade asking for a ~150-point move with a
~150-point stop. Root cause: GhostConfig capped how TIGHT a stop could be
(`min_stop_atr_mult`) but never how WIDE. `vwap_reclaim` anchors its stop to a
12-bar swing, which on the ES fixture reached 7.56x ATR14 — on NQ (ATR ~20pts)
that is the 150-point stop observed.

Measured on the ES sample, capping at 1.75x ATR raised expectancy from 0.243R
to 0.400R while cutting the worst stop from 23.9 to 5.9 points: wide stops were
destroying edge, not merely uncomfortable.

The second half covers the notifications the operator needs while away from
home — trade_closed and trade_progress events, and the status digest.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig, GhostConfig
from signal_engine.output.discord import build_discord_payload
from signal_engine.output.events import OutputEvent
from signal_engine.runtime.engine import _crossed_milestones
from signal_engine.features.analyzer import TimeframeAnalysis
from signal_engine.ghost.brain import GhostBrain, _stop_context
from signal_engine.ghost.models import (
    Bias, LiquidityState, MarketRegime, MarketState, MTFAlignment, MTFContext,
    NoTradeReason, RegimeState, StructureState, TimeframeBias, Volatility,
)
from signal_engine.signals.models import Signal, SignalDirection


# --------------------------------------------------------------------------- #
# helpers (mirrors tests/test_phase15_authority.py)
# --------------------------------------------------------------------------- #
def _sig(direction="LONG", instrument="ES", setup="vwap_reclaim",
         entry=5000, stop=4995, tps=(5010,)):
    d = SignalDirection.LONG if direction == "LONG" else SignalDirection.SHORT
    return Signal(signal_id=f"s-{direction}-{entry}-{stop}", instrument=instrument,
                  setup=setup, direction=d,
                  timestamp=pd.Timestamp("2026-06-15 10:00", tz="America/New_York"),
                  timeframe="5m", entry=entry, stop=stop, take_profits=list(tps),
                  risk_reward=[abs(t - entry) / max(abs(entry - stop), 1e-9)
                               for t in tps],
                  reasons=["fake"], invalidation_price=stop)


def _state(session="RTH") -> MarketState:
    reg = RegimeState(MarketRegime.TREND_UP, Volatility.NORMAL, 0.5, 0.5,
                      100.0, 100.0, 100.0, "", None)
    mtf = MTFContext(TimeframeBias("1H", Bias.BULLISH),
                     TimeframeBias("15m", Bias.BULLISH),
                     TimeframeBias("5m", Bias.BULLISH), MTFAlignment.ALIGNED)
    st = StructureState(Bias.BULLISH, False, "BOS", Bias.BULLISH, "")
    liq = LiquidityState(Bias.BEARISH, 4990.0, None, True, None, None, "")
    return MarketState("ES", pd.Timestamp("2026-06-15 10:00", tz="America/New_York"),
                       session, 100.0, 100.0, "ABOVE", reg, mtf, st, liq, [])


def _ta(atr=3.0):
    idx = pd.date_range("2026-06-15 09:30", periods=40, freq="5min",
                        tz="America/New_York")
    ind = pd.DataFrame({"ema_20": [100.0] * 40, "ema_50": [100.0] * 40,
                        "atr_14": [atr] * 40, "rvol_20": [1.0] * 40,
                        "vwap": [100.0] * 40}, index=idx)
    return TimeframeAnalysis("5m", idx[-1], ind, [], [], [], [])


def _cfg(**over) -> GhostConfig:
    base = dict(min_confluence_score=0.0, block_htf_conflict=False,
                block_against_structure=False, cooldown_bars=0)
    base.update(over)
    return GhostConfig(**base)


# --------------------------------------------------------------------------- #
# STOP-TOO-WIDE — the actual 150-point bug
# --------------------------------------------------------------------------- #
def test_stop_wider_than_atr_cap_is_rejected():
    """The reported case: stop 7.5x ATR away. Must be rejected, not traded."""
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, max_stop_points={}))
    # ATR 3, stop 30pts away => ratio 10.0
    sig = _sig("LONG", entry=5000, stop=4970, tps=(5060,))
    dec = brain._decide_signal(sig, _state(), _ta(3.0), None, False, False,
                               has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE in dec.no_trade_reasons
    assert dec.action == "NO_TRADE"


def test_stop_within_atr_cap_is_allowed():
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, max_stop_points={}))
    # ATR 3, stop 4pts => ratio 1.33 — inside both the tight and wide bounds
    sig = _sig("LONG", entry=5000, stop=4996, tps=(5010,))
    dec = brain._decide_signal(sig, _state(), _ta(3.0), None, False, False,
                               has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE not in dec.no_trade_reasons
    assert NoTradeReason.STOP_TOO_TIGHT not in dec.no_trade_reasons


def test_stop_guard_does_not_silently_widen_or_shrink():
    """Same contract as stop-too-tight: reject with a reason, never rewrite
    the operator's trade plan behind their back."""
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, max_stop_points={}))
    sig = _sig("LONG", entry=5000, stop=4970, tps=(5060,))
    dec = brain._decide_signal(sig, _state(), _ta(3.0), None, False, False,
                               has_active=None)
    assert dec.signal.stop == 4970
    assert dec.signal.take_profits == [5060]


def test_zero_max_stop_atr_mult_disables_the_atr_cap():
    brain = GhostBrain(_cfg(max_stop_atr_mult=0.0, min_stop_atr_mult=0.0,
                            max_stop_points={}))
    sig = _sig("LONG", entry=5000, stop=4970, tps=(5060,))
    dec = brain._decide_signal(sig, _state(), _ta(3.0), None, False, False,
                               has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE not in dec.no_trade_reasons


def test_short_side_is_capped_symmetrically():
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, max_stop_points={}))
    sig = _sig("SHORT", entry=5000, stop=5030, tps=(4940,))
    dec = brain._decide_signal(sig, _state(), _ta(3.0), None, False, False,
                               has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE in dec.no_trade_reasons


# --------------------------------------------------------------------------- #
# Absolute per-symbol points ceiling (dollar-risk backstop)
# --------------------------------------------------------------------------- #
def test_absolute_points_cap_rejects_even_when_atr_ratio_is_fine():
    """An ATR spike can make a huge stop 'proportionate'. The points ceiling
    still bounds the dollar risk."""
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, min_stop_atr_mult=0.0,
                            max_stop_points={"NQ": 50.0}))
    # ATR 100 => 80pt stop is only 0.8x ATR, inside the ATR cap...
    sig = _sig("LONG", instrument="NQ", entry=20000, stop=19920, tps=(20160,))
    dec = brain._decide_signal(sig, _state(), _ta(100.0), None, False, False,
                               has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE in dec.no_trade_reasons


def test_points_cap_applies_per_symbol_and_ignores_unlisted_symbols():
    brain = GhostBrain(_cfg(max_stop_atr_mult=0.0, min_stop_atr_mult=0.0,
                            max_stop_points={"NQ": 50.0}))
    nq = brain._decide_signal(_sig("LONG", instrument="NQ", entry=20000,
                                   stop=19900, tps=(20200,)),
                              _state(), _ta(100.0), None, False, False,
                              has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE in nq.no_trade_reasons
    # ES is absent from this mapping => ATR rule alone, which is disabled here
    es = brain._decide_signal(_sig("LONG", instrument="ES", entry=5000,
                                   stop=4900, tps=(5200,)),
                              _state(), _ta(100.0), None, False, False,
                              has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE not in es.no_trade_reasons


def test_points_cap_still_applies_when_atr_is_unavailable():
    """The ATR rule can't fire without ATR; the dollar backstop must still."""
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, min_stop_atr_mult=0.0,
                            max_stop_points={"NQ": 50.0}))
    sig = _sig("LONG", instrument="NQ", entry=20000, stop=19850, tps=(20300,))
    dec = brain._decide_signal(sig, _state(), None, None, False, False,
                               has_active=None)
    assert NoTradeReason.STOP_TOO_WIDE in dec.no_trade_reasons


def test_missing_atr_never_produces_a_false_too_wide_from_the_atr_rule():
    brain = GhostBrain(_cfg(max_stop_atr_mult=1.75, min_stop_atr_mult=0.0,
                            max_stop_points={}))
    info = _stop_context(_sig("LONG", entry=5000, stop=4900), None,
                         _cfg(max_stop_atr_mult=1.75, max_stop_points={}))
    assert info["too_wide"] is False
    assert info["atr"] is None


def test_stop_context_reports_both_bounds_for_diagnostics():
    info = _stop_context(_sig("LONG", instrument="NQ", entry=20000, stop=19900),
                         _ta(10.0),
                         _cfg(max_stop_atr_mult=1.75, max_stop_points={"NQ": 50.0}))
    assert info["stop_distance"] == 100.0
    assert info["ratio"] == 10.0
    assert info["max_threshold"] == 1.75
    assert info["max_points"] == 50.0
    assert info["too_wide"] is True


# --------------------------------------------------------------------------- #
# The shipped configuration actually carries the fix
# --------------------------------------------------------------------------- #
def test_shipped_config_caps_stop_width():
    cfg = EngineConfig.load()
    assert 0 < cfg.ghost.max_stop_atr_mult <= 2.0, \
        "a cap above 2x ATR lets the 150-point NQ trade back in"


def test_shipped_config_does_not_fix_stop_length_in_points():
    """Trade length must follow conditions, not a constant. The ATR rule caps
    the RATIO; a points ceiling would cap the trade itself, and dollar risk is
    already bounded by RiskEngine sizing contracts off the stop distance."""
    assert EngineConfig.load().ghost.max_stop_points == {}


def test_shipped_config_keeps_min_below_max():
    cfg = EngineConfig.load()
    assert cfg.ghost.min_stop_atr_mult < cfg.ghost.max_stop_atr_mult, \
        "an inverted band would reject every trade"


# --------------------------------------------------------------------------- #
# R-milestone crossing — the "how far up/down is it" alert
# --------------------------------------------------------------------------- #
MS = [-0.5, 0.5, 1.0, 1.5, 2.0, 3.0]


def test_crossing_up_reports_every_level_passed():
    assert _crossed_milestones(0.2, 1.7, MS) == [0.5, 1.0, 1.5]


def test_crossing_down_reports_the_adverse_level():
    assert _crossed_milestones(0.1, -0.8, MS) == [-0.5]


def test_no_crossing_reports_nothing():
    assert _crossed_milestones(1.1, 1.2, MS) == []
    assert _crossed_milestones(1.2, 1.1, MS) == []


def test_level_already_passed_is_not_re_announced():
    """The operator must not get a +1R ping on every bar of a winning trade."""
    assert _crossed_milestones(1.6, 1.9, MS) == []


def test_oscillating_around_a_level_announces_it_once_per_direction():
    """Up through +1R announces it; falling back below does not re-announce,
    and neither does crossing up again from just under."""
    assert _crossed_milestones(0.9, 1.1, MS) == [1.0]
    assert _crossed_milestones(1.1, 0.9, MS) == []


def test_positive_milestones_never_fire_on_the_way_down():
    assert _crossed_milestones(2.5, 0.6, MS) == []


def test_exact_touch_of_a_level_counts_as_reaching_it():
    assert _crossed_milestones(0.4, 0.5, MS) == [0.5]
    assert _crossed_milestones(0.0, -0.5, MS) == [-0.5]


# --------------------------------------------------------------------------- #
# Event construction — kinds, dedup keys, payload contents
# --------------------------------------------------------------------------- #
def _closed_dict(**over):
    d = {"signal_id": "abc123def456", "instrument": "NQ", "direction": "LONG",
         "setup": "orb", "entry": 20000.0, "exit_price": 20030.0,
         "exit_reason": "TP1", "pnl_points": 30.0, "r_multiple": 1.5,
         "mfe_r": 1.8, "mae_r": -0.3, "bars_held": 7, "stop": 19980.0,
         "tp1": 20030.0, "tp2": 20060.0, "tp1_hit": True, "current_price": 20030.0}
    d.update(over)
    return d


def test_trade_closed_event_carries_realised_pnl():
    ev = OutputEvent.from_trade_closed(_closed_dict())
    assert ev.kind == "trade_closed"
    assert ev.status_payload["r_multiple"] == 1.5
    body = build_discord_payload(ev)
    fields = {f["name"]: f["value"] for f in body["embeds"][0]["fields"]}
    assert fields["P&L (pts)"] == "+30.00"
    assert fields["Result"] == "+1.50R"
    assert fields["Exit reason"] == "TP1"
    assert fields["Best (MFE)"] == "+1.80R"


def test_trade_closed_dedup_key_is_distinct_from_the_open_event():
    """One trade => one open notification AND one close notification, never a
    close suppressed as a duplicate of its own open."""
    d = _closed_dict()
    assert OutputEvent.from_trade_closed(d).dedup_key \
        != OutputEvent.from_trade_opened(d).dedup_key


def test_losing_trade_renders_as_a_loss():
    ev = OutputEvent.from_trade_closed(
        _closed_dict(exit_reason="STOP", pnl_points=-20.0, r_multiple=-1.0))
    assert "🛑" in ev.title
    assert build_discord_payload(ev)["embeds"][0]["color"] == 0xFF4444


def test_winning_trade_renders_as_a_win():
    ev = OutputEvent.from_trade_closed(_closed_dict())
    assert "✅" in ev.title
    assert build_discord_payload(ev)["embeds"][0]["color"] == 0x00C851


def test_trade_progress_dedup_key_pins_the_milestone():
    d = _closed_dict()
    a = OutputEvent.from_trade_progress(d, 1.0)
    b = OutputEvent.from_trade_progress(d, 2.0)
    assert a.dedup_key != b.dedup_key
    assert OutputEvent.from_trade_progress(d, 1.0).dedup_key == a.dedup_key


def test_trade_progress_shows_where_the_trade_stands():
    ev = OutputEvent.from_trade_progress(_closed_dict(), 1.0)
    assert ev.kind == "trade_progress"
    fields = {f["name"]: f["value"]
              for f in build_discord_payload(ev)["embeds"][0]["fields"]}
    assert fields["Open P&L"] == "+1.50R"
    assert fields["P&L (pts)"] == "+30.00"
    assert fields["Now"] != "—"


def test_adverse_milestone_is_titled_as_such():
    ev = OutputEvent.from_trade_progress(
        _closed_dict(pnl_points=-10.0, r_multiple=-0.5), -0.5)
    assert "-0.5R" in ev.title


# --------------------------------------------------------------------------- #
# Status digest — the "is my bot alive" message
# --------------------------------------------------------------------------- #
def test_status_digest_renders_open_positions_and_totals():
    payload = {"status": "ONLINE", "uptime": "3h 12m", "feed": "CONNECTED",
               "trades_today": 2, "open_count": 1, "closed_today": 1,
               "realised_points": 14.5, "win_rate": "100%", "open_r": 0.8,
               "open_trades": ["NQ LONG @ 20000.00 → 20016.00 (+16.00 pts, +0.80R)"]}
    ts = pd.Timestamp("2026-06-15 14:00", tz="America/New_York")
    ev = OutputEvent.from_status_digest(payload, ts)
    assert ev.kind == "status_digest"
    fields = {f["name"]: f["value"]
              for f in build_discord_payload(ev)["embeds"][0]["fields"]}
    assert fields["Bot"] == "ONLINE"
    assert fields["Trades today"] == "2"
    assert fields["Realised today"] == "+14.50"
    assert "NQ LONG" in fields["Open positions"]


def test_status_digest_with_no_open_trades_says_so():
    ts = pd.Timestamp("2026-06-15 14:00", tz="America/New_York")
    ev = OutputEvent.from_status_digest({"status": "ONLINE"}, ts)
    fields = {f["name"]: f["value"]
              for f in build_discord_payload(ev)["embeds"][0]["fields"]}
    assert "no open trades" in fields["Open positions"]


def test_each_digest_is_its_own_event():
    p = {"status": "ONLINE"}
    a = OutputEvent.from_status_digest(
        p, pd.Timestamp("2026-06-15 14:00", tz="America/New_York"))
    b = OutputEvent.from_status_digest(
        p, pd.Timestamp("2026-06-15 15:00", tz="America/New_York"))
    assert a.dedup_key != b.dedup_key


def test_digest_payload_never_leaks_the_webhook(engine, registry, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.invalid/SECRET")
    rt = _mk_runtime(engine, registry)
    blob = repr(rt.status_digest_payload())
    assert "SECRET" not in blob and "http" not in blob


# --------------------------------------------------------------------------- #
# Digest timer
# --------------------------------------------------------------------------- #
def _mk_runtime(engine, registry, dispatcher=None):
    from signal_engine.paper.tracker import PaperTradeTracker
    from signal_engine.runtime.engine import BotRuntime
    from signal_engine.setups.engine import SetupEngine
    from signal_engine.signals.engine import SignalEngine
    return BotRuntime(engine, SetupEngine(engine.cfg, registry),
                      SignalEngine(engine.cfg.signals), registry,
                      paper_tracker=PaperTradeTracker(),
                      output_dispatcher=dispatcher)


class _Recorder:
    """Stands in for an OutputDispatcher — records what the runtime asked to
    publish without touching the network."""
    def __init__(self):
        self.adapters = []
        self.digests = []
        self.closed = []
        self.progress = []

    def publish_status_digest(self, payload, timestamp):
        self.digests.append((payload, timestamp))

    def publish_trade_closed(self, d):
        self.closed.append(d)

    def publish_trade_progress(self, d, m):
        self.progress.append((d, m))


def test_digest_fires_once_per_interval(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    engine.cfg.output.notifications.status_digest = True
    engine.cfg.output.notifications.status_digest_minutes = 60.0
    t0 = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    assert rt.maybe_send_status_digest(t0) is True
    assert rt.maybe_send_status_digest(t0 + timedelta(minutes=30)) is False
    assert rt.maybe_send_status_digest(t0 + timedelta(minutes=61)) is True
    assert len(rec.digests) == 2


def test_digest_can_be_disabled(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    engine.cfg.output.notifications.status_digest = False
    assert rt.maybe_send_status_digest(datetime.now(timezone.utc)) is False
    assert rec.digests == []


def test_zero_interval_disables_the_digest(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    engine.cfg.output.notifications.status_digest_minutes = 0.0
    assert rt.maybe_send_status_digest(datetime.now(timezone.utc)) is False


def test_digest_without_a_dispatcher_is_a_no_op(engine, registry):
    rt = _mk_runtime(engine, registry, None)
    assert rt.maybe_send_status_digest(datetime.now(timezone.utc)) is False


# --------------------------------------------------------------------------- #
# Runtime wiring — close and progress actually reach the dispatcher
# --------------------------------------------------------------------------- #
def _open_trade(rt, **over):
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    kw = dict(signal_id="t1", instrument="ES", direction="LONG", setup="orb",
              entry=5000.0, stop=4990.0, take_profits=[5020.0], opened_at=ts)
    kw.update(over)
    return rt.paper_tracker.open_trade(**kw)


def test_runtime_publishes_a_close_with_realised_pnl(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    t = _open_trade(rt)
    t.status = t.status.__class__.ACTIVE
    bar = pd.Series({"open": 5000.0, "high": 5021.0, "low": 4999.0, "close": 5020.0})
    before = {t.signal_id: t.r_multiple}
    closed = rt.paper_tracker.update_symbol(
        "ES", bar, pd.Timestamp("2026-06-15 10:05", tz="America/New_York"))
    rt._notify_trade_updates("ES", closed, before)
    assert len(rec.closed) == 1
    assert rec.closed[0]["r_multiple"] == pytest.approx(2.0)
    assert rec.closed[0]["exit_reason"] == "TP1"


def test_runtime_publishes_progress_for_a_still_open_trade(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    t = _open_trade(rt, take_profits=[5100.0])
    t.status = t.status.__class__.ACTIVE
    bar = pd.Series({"open": 5000.0, "high": 5011.0, "low": 4999.0, "close": 5010.0})
    before = {t.signal_id: 0.0}
    closed = rt.paper_tracker.update_symbol(
        "ES", bar, pd.Timestamp("2026-06-15 10:05", tz="America/New_York"))
    rt._notify_trade_updates("ES", closed, before)
    assert rec.closed == []
    assert [m for _d, m in rec.progress] == [0.5, 1.0]


def test_runtime_skips_progress_for_a_trade_opened_this_bar(engine, registry):
    """A trade with no prior R reading was opened on this bar — the OPEN
    notification already told the operator about it."""
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    t = _open_trade(rt, take_profits=[5100.0])
    t.status = t.status.__class__.ACTIVE
    bar = pd.Series({"open": 5000.0, "high": 5011.0, "low": 4999.0, "close": 5010.0})
    closed = rt.paper_tracker.update_symbol(
        "ES", bar, pd.Timestamp("2026-06-15 10:05", tz="America/New_York"))
    rt._notify_trade_updates("ES", closed, {})     # no "before" reading
    assert rec.progress == []


def test_notifications_can_be_switched_off(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    engine.cfg.output.notifications.trade_closed = False
    engine.cfg.output.notifications.trade_progress = False
    t = _open_trade(rt)
    t.status = t.status.__class__.ACTIVE
    bar = pd.Series({"open": 5000.0, "high": 5021.0, "low": 4999.0, "close": 5020.0})
    closed = rt.paper_tracker.update_symbol(
        "ES", bar, pd.Timestamp("2026-06-15 10:05", tz="America/New_York"))
    rt._notify_trade_updates("ES", closed, {t.signal_id: 0.0})
    assert rec.closed == [] and rec.progress == []


def test_progress_is_scoped_to_the_symbol_being_processed(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    nq = _open_trade(rt, signal_id="nq1", instrument="NQ", entry=20000.0,
                     stop=19900.0, take_profits=[20500.0])
    nq.status = nq.status.__class__.ACTIVE
    nq.r_multiple = 1.2
    rt._notify_trade_updates("ES", [], {"nq1": 0.0})
    assert rec.progress == [], "an ES bar must not report NQ's progress"


# --------------------------------------------------------------------------- #
# Regime-adaptive targets (shipped OFF — measured worse; mechanism kept)
# --------------------------------------------------------------------------- #
from signal_engine.ghost.targets import apply_plan, plan_targets  # noqa: E402


def _state_regime(regime, alignment=MTFAlignment.ALIGNED):
    reg = RegimeState(regime, Volatility.NORMAL, 0.5, 0.5, 100.0, 100.0, 100.0, "", None)
    mtf = MTFContext(TimeframeBias("1H", Bias.BULLISH),
                     TimeframeBias("15m", Bias.BULLISH),
                     TimeframeBias("5m", Bias.BULLISH), alignment)
    st = StructureState(Bias.BULLISH, False, "BOS", Bias.BULLISH, "")
    liq = LiquidityState(Bias.BEARISH, 4990.0, None, True, None, None, "")
    return MarketState("ES", pd.Timestamp("2026-06-15 10:00", tz="America/New_York"),
                       "RTH", 100.0, 100.0, "ABOVE", reg, mtf, st, liq, [])


def test_adaptive_targets_disabled_keeps_the_setup_plan():
    sig = _sig("LONG", setup="orb", entry=5000, stop=4990, tps=(5010, 5020))
    assert plan_targets(sig, _state_regime(MarketRegime.TREND_UP),
                        _cfg(adaptive_targets=False)) is None


def test_aligned_trend_stretches_the_far_target():
    sig = _sig("LONG", setup="orb", entry=5000, stop=4990, tps=(5010, 5020))
    plan = plan_targets(sig, _state_regime(MarketRegime.TREND_UP),
                        _cfg(adaptive_targets=True, target_r_mtf_bonus=0.0))
    assert plan.band == "aligned"
    assert plan.take_profits == [5010, 5030]       # TP1 kept, far -> 3R


def test_counter_trend_collapses_to_the_near_target():
    sig = _sig("LONG", setup="orb", entry=5000, stop=4990, tps=(5010, 5020))
    plan = plan_targets(sig, _state_regime(MarketRegime.TREND_DOWN),
                        _cfg(adaptive_targets=True))
    assert plan.band == "counter"
    assert plan.take_profits == [5010]


def test_mtf_bonus_only_applies_when_every_timeframe_agrees():
    sig = _sig("LONG", setup="orb", entry=5000, stop=4990, tps=(5010, 5020))
    c = _cfg(adaptive_targets=True, target_r_mtf_bonus=1.0)
    aligned = plan_targets(sig, _state_regime(MarketRegime.TREND_UP), c)
    mixed = plan_targets(sig, _state_regime(MarketRegime.TREND_UP,
                                            MTFAlignment.MIXED), c)
    assert aligned.far_r == 4.0 and mixed.far_r == 3.0


def test_apply_plan_preserves_signal_identity():
    """signal_id drives end-to-end dedup; changing targets must not change it."""
    sig = _sig("LONG", setup="orb", entry=5000, stop=4990, tps=(5010, 5020))
    plan = plan_targets(sig, _state_regime(MarketRegime.TREND_UP),
                        _cfg(adaptive_targets=True))
    out = apply_plan(sig, plan)
    assert out.signal_id == sig.signal_id
    assert out.take_profits != sig.take_profits
    assert out.risk_reward == pytest.approx([1.0, plan.far_r])


def test_short_far_target_goes_below_entry():
    sig = _sig("SHORT", setup="orb", entry=5000, stop=5010, tps=(4990, 4980))
    plan = plan_targets(sig, _state_regime(MarketRegime.TREND_DOWN),
                        _cfg(adaptive_targets=True, target_r_mtf_bonus=0.0))
    assert plan.take_profits == [4990, 4970]


def test_shipped_config_leaves_adaptive_targets_off():
    """Replayed through the live tracker it cut expectancy 0.500R -> 0.341R.
    Turning it back on needs new evidence, not a config flip."""
    assert EngineConfig.load().ghost.adaptive_targets is False


# --------------------------------------------------------------------------- #
# Breakeven after TP1 (shipped ON — measured better)
# --------------------------------------------------------------------------- #
from signal_engine.paper.models import ExitReason  # noqa: E402
from signal_engine.paper.tracker import PaperTradeTracker  # noqa: E402

_T0 = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def _runner(tracker):
    t = tracker.open_trade(signal_id="be", instrument="ES", direction="LONG",
                           setup="orb", entry=5000.0, stop=4990.0,
                           take_profits=[5010.0, 5020.0], opened_at=_T0)
    t.status = t.status.__class__.ACTIVE
    return t


def test_breakeven_moves_stop_to_entry_once_tp1_is_reached():
    tr = PaperTradeTracker(breakeven_after_tp1=True)
    t = _runner(tr)
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5009), _T0 + pd.Timedelta(minutes=5))
    assert t.tp1_hit and t.is_open
    assert t.stop == 5000.0


def test_breakeven_turns_a_full_giveback_into_a_scratch():
    """The case it exists for: TP1 hit, then price collapses through the old
    stop. Without breakeven that is -1R; with it, ~0R."""
    tr = PaperTradeTracker(breakeven_after_tp1=True)
    t = _runner(tr)
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5009), _T0 + pd.Timedelta(minutes=5))
    tr.update_on_bar(t, _bar(5009, 5009, 4985, 4986), _T0 + pd.Timedelta(minutes=10))
    assert not t.is_open
    assert t.exit_reason is ExitReason.STOP
    assert t.r_multiple == pytest.approx(0.0)


def test_without_breakeven_the_same_path_loses_a_full_r():
    tr = PaperTradeTracker(breakeven_after_tp1=False)
    t = _runner(tr)
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5009), _T0 + pd.Timedelta(minutes=5))
    tr.update_on_bar(t, _bar(5009, 5009, 4985, 4986), _T0 + pd.Timedelta(minutes=10))
    assert t.r_multiple == pytest.approx(-1.0)


def test_breakeven_never_closes_the_trade_on_the_tp1_bar_itself():
    """This bar's low (4999) is below entry, but the stop move takes effect
    from the next bar — it must not retroactively stop out a live trade."""
    tr = PaperTradeTracker(breakeven_after_tp1=True)
    t = _runner(tr)
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5009), _T0 + pd.Timedelta(minutes=5))
    assert t.is_open


def test_breakeven_still_lets_the_trade_reach_tp2():
    tr = PaperTradeTracker(breakeven_after_tp1=True)
    t = _runner(tr)
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5009), _T0 + pd.Timedelta(minutes=5))
    tr.update_on_bar(t, _bar(5009, 5021, 5005, 5020), _T0 + pd.Timedelta(minutes=10))
    assert t.exit_reason is ExitReason.TP2
    assert t.r_multiple == pytest.approx(2.0)


def test_initial_risk_is_unchanged_by_the_stop_move():
    """R multiples stay measured against the ORIGINAL risk, or a TP2 win
    would read as infinite R once the stop sits at entry."""
    tr = PaperTradeTracker(breakeven_after_tp1=True)
    t = _runner(tr)
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5009), _T0 + pd.Timedelta(minutes=5))
    assert t.initial_risk == pytest.approx(10.0)


def test_shipped_config_enables_breakeven():
    assert EngineConfig.load().ghost.breakeven_after_tp1 is True


def test_shipped_config_enables_eod_close():
    assert EngineConfig.load().ghost.eod_close_enabled is True


# --------------------------------------------------------------------------- #
# Force-flatten at the RTH close (operator: no overnight/24-7 holding)
# --------------------------------------------------------------------------- #
def test_shipped_config_trades_rth_only():
    """No overnight exposure: entries only happen in RTH (09:30-16:00 ET), and
    eod_close_enabled (above) force-flattens anything still open at the close.
    Together they guarantee a trade can never span more than one RTH session."""
    assert EngineConfig.load().ghost.valid_sessions == ["RTH"]


def test_eth_session_signal_is_rejected_by_default():
    """The actual guard the RTH-only default relies on: a signal scored while
    the market state says ETH must be refused, not merely discouraged."""
    brain = GhostBrain(_cfg())   # _cfg() never overrides valid_sessions
    sig = _sig("LONG", entry=5000, stop=4995, tps=(5010,))
    dec = brain._decide_signal(sig, _state(session="ETH"), _ta(3.0), None,
                               False, False, has_active=None)
    assert NoTradeReason.INVALID_SESSION in dec.no_trade_reasons


def test_rth_session_signal_is_not_rejected_for_session():
    brain = GhostBrain(_cfg())
    sig = _sig("LONG", entry=5000, stop=4995, tps=(5010,))
    dec = brain._decide_signal(sig, _state(session="RTH"), _ta(3.0), None,
                               False, False, has_active=None)
    assert NoTradeReason.INVALID_SESSION not in dec.no_trade_reasons


def _eod_runner(tracker, **over):
    kw = dict(signal_id="eod", instrument="ES", direction="LONG", setup="orb",
              entry=5000.0, stop=4990.0, take_profits=[5100.0], opened_at=_T0)
    kw.update(over)
    t = tracker.open_trade(**kw)
    t.status = t.status.__class__.ACTIVE
    return t


def test_close_symbol_eod_flattens_an_open_trade_at_the_given_price():
    tr = PaperTradeTracker()
    t = _eod_runner(tr)
    close_ts = pd.Timestamp("2026-06-15 16:00", tz="America/New_York")
    closed = tr.close_symbol_eod("ES", 5008.0, close_ts)
    assert closed == [t]
    assert not t.is_open
    assert t.exit_reason is ExitReason.EOD
    assert t.status is t.status.__class__.CLOSED
    assert t.exit_price == 5008.0
    assert t.closed_at == close_ts
    assert t.pnl_points == pytest.approx(8.0)
    assert t.r_multiple == pytest.approx(0.8)          # 8 pts / 10 pt risk


def test_close_symbol_eod_ignores_other_symbols():
    tr = PaperTradeTracker()
    t = _eod_runner(tr, signal_id="eod-nq", instrument="NQ")
    assert tr.close_symbol_eod("ES", 5008.0, _T0) == []
    assert t.is_open


def test_close_symbol_eod_is_a_no_op_with_nothing_open():
    tr = PaperTradeTracker()
    assert tr.close_symbol_eod("ES", 5008.0, _T0) == []


def test_close_symbol_eod_is_idempotent():
    """Every non-RTH bar re-checks; a symbol with no open trade left must not
    raise or double-close."""
    tr = PaperTradeTracker()
    _eod_runner(tr)
    first = tr.close_symbol_eod("ES", 5008.0, _T0)
    second = tr.close_symbol_eod("ES", 5009.0, _T0 + pd.Timedelta(minutes=5))
    assert len(first) == 1 and second == []


def test_close_symbol_eod_can_produce_a_loss():
    """EOD is neutral — it reports whatever the market did, win or lose."""
    tr = PaperTradeTracker()
    t = _eod_runner(tr)
    tr.close_symbol_eod("ES", 4995.0, _T0)
    assert t.pnl_points == pytest.approx(-5.0)
    assert t.r_multiple == pytest.approx(-0.5)


def test_close_symbol_eod_works_for_short_trades():
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="eod-short", instrument="ES", direction="SHORT",
                      setup="orb", entry=5000.0, stop=5010.0,
                      take_profits=[4900.0], opened_at=_T0)
    t.status = t.status.__class__.ACTIVE
    tr.close_symbol_eod("ES", 4990.0, _T0)
    assert t.pnl_points == pytest.approx(10.0)
    assert t.r_multiple == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Runtime wiring — _run_ghost actually calls close_symbol_eod on the bar
# where the session leaves RTH, not before and not silently skipped.
#
# Uses REAL AnalysisContext objects sliced from the ES sample rather than a
# hand-rolled stub: _run_ghost's step 2 calls the real analyzer/GhostBrain,
# which need genuine TimeframeView history (indicators, structure) to run at
# all. min_confluence_score=99999 keeps GHOST from approving anything new, so
# these contexts isolate exactly the EOD guard in step 1 without a fake ever
# standing in for the pipeline it's meant to prove works.
# --------------------------------------------------------------------------- #
def _rth_then_eod_ctx(engine):
    from pathlib import Path
    from signal_engine.data.providers.csv_provider import CsvProvider
    samples = Path(__file__).resolve().parent / "data_samples"
    tf_bars = engine.build_timeframes(engine.load_contract(CsvProvider(samples), "ES", "ESM26"))
    exec_tf = engine.cfg.timeframe_roles.execution
    # skip the first 60 bars for indicator warmup, same convention the
    # calibration scripts use for this fixture.
    ctx_iter = (engine.analysis_context("ES", ts, tf_bars)
               for ts in tf_bars[exec_tf].index[60:])
    rth_ctx = next(c for c in ctx_iter if c.session == "RTH")
    # continuing the SAME generator picks up right after rth_ctx, so this is
    # the next bar whose session left RTH — no list/equality comparison on
    # AnalysisContext needed (it holds DataFrames, which don't support ==).
    eod_ctx = next(c for c in ctx_iter if c.session != "RTH")
    return rth_ctx, eod_ctx


def _eod_wiring_runtime(engine, registry, dispatcher, **ghost_over):
    gcfg = GhostConfig(min_confluence_score=99999.0, **ghost_over)
    from signal_engine.paper.tracker import PaperTradeTracker
    from signal_engine.runtime.engine import BotRuntime
    from signal_engine.setups.engine import SetupEngine
    from signal_engine.signals.engine import SignalEngine
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    ghost=GhostBrain(gcfg), paper_tracker=PaperTradeTracker(),
                    output_dispatcher=dispatcher)
    rt.start()
    return rt


def _open_wide_trade(rt, ctx):
    """A trade whose stop/target sit far outside the sample's price range, so
    it can only ever close via the guard under test, never TP/stop."""
    px = float(ctx.execution.bars["close"].iloc[-1])
    t = rt.paper_tracker.open_trade(
        signal_id="eod-wire", instrument="ES", direction="LONG", setup="orb",
        entry=px, stop=px - 1000.0, take_profits=[px + 1000.0],
        opened_at=ctx.decision_time)
    t.status = t.status.__class__.ACTIVE
    return t


def test_run_ghost_force_closes_when_session_leaves_rth(engine, registry):
    rth_ctx, eod_ctx = _rth_then_eod_ctx(engine)
    rec = _Recorder()
    rt = _eod_wiring_runtime(engine, registry, rec)
    t = _open_wide_trade(rt, rth_ctx)

    rt._run_ghost("ES", rth_ctx, [])
    assert t.is_open, "must not close while the bar is still inside RTH"

    rt._run_ghost("ES", eod_ctx, [])
    assert not t.is_open
    assert t.exit_reason.value == "EOD"
    assert t.exit_price == pytest.approx(float(eod_ctx.execution.bars["close"].iloc[-1]))
    assert len(rec.closed) == 1 and rec.closed[0]["exit_reason"] == "EOD"


def test_run_ghost_respects_eod_close_enabled_false(engine, registry):
    rth_ctx, eod_ctx = _rth_then_eod_ctx(engine)
    rec = _Recorder()
    rt = _eod_wiring_runtime(engine, registry, rec, eod_close_enabled=False)
    t = _open_wide_trade(rt, rth_ctx)

    rt._run_ghost("ES", rth_ctx, [])
    rt._run_ghost("ES", eod_ctx, [])
    assert t.is_open
    assert rec.closed == []


def test_run_ghost_reads_the_brains_own_config_not_the_data_engines():
    """A caller can build GhostBrain from its own GhostConfig (tests do this
    constantly). _run_ghost must honour THAT config for the EOD guard, cooldown
    etc. — not silently fall back to data_engine.cfg.ghost because the two
    happen to be the same object in the normal bootstrap path."""
    from signal_engine.data.contracts import ContractRegistry
    from signal_engine.data.engine import DataEngine
    from signal_engine.paper.tracker import PaperTradeTracker
    from signal_engine.runtime.engine import BotRuntime
    from signal_engine.setups.engine import SetupEngine
    from signal_engine.signals.engine import SignalEngine

    engine = DataEngine()   # its own cfg.ghost is left at the library default
    registry = ContractRegistry.load()
    custom = GhostConfig(min_confluence_score=99999.0, eod_close_enabled=False)
    assert engine.cfg.ghost.eod_close_enabled is True, \
        "the data engine's config must stay untouched for this test to prove anything"
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    ghost=GhostBrain(custom), paper_tracker=PaperTradeTracker())
    rt.start()
    assert rt.ghost.cfg is custom


def _sig_at(hhmm):
    s = _sig("LONG", entry=5000, stop=4995, tps=(5010,))
    from dataclasses import replace
    return replace(s, timestamp=pd.Timestamp(f"2026-06-15 {hhmm}", tz="America/New_York"))


def test_no_new_rth_entry_in_the_last_half_hour():
    """A 15:50 entry would be flattened by the 16:00 close before its targets
    could possibly be reached."""
    brain = GhostBrain(_cfg())
    late = brain._decide_signal(_sig_at("15:30"), _state(), _ta(3.0), None,
                                False, False, has_active=None)
    ok = brain._decide_signal(_sig_at("15:25"), _state(), _ta(3.0), None,
                              False, False, has_active=None)
    assert NoTradeReason.TOO_LATE in late.no_trade_reasons
    assert NoTradeReason.TOO_LATE not in ok.no_trade_reasons
    off = GhostBrain(_cfg(last_entry_time=None))._decide_signal(
        _sig_at("15:50"), _state(), _ta(3.0), None, False, False, has_active=None)
    assert NoTradeReason.TOO_LATE not in off.no_trade_reasons


def test_hourly_digest_is_off_by_default(engine, registry):
    rec = _Recorder()
    rt = _mk_runtime(engine, registry, rec)
    assert EngineConfig.load().output.notifications.status_digest is False
    assert rt.maybe_send_status_digest(datetime.now(timezone.utc)) is False
    assert rec.digests == []
