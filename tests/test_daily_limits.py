"""Daily limits on the operator's 5-MNQ position, whole-contract partials,
and the messages that say so."""
from types import MethodType, SimpleNamespace

import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig
from signal_engine.output.discord import build_discord_payload
from signal_engine.output.events import OutputEvent
from signal_engine.paper.tracker import PaperTradeTracker, whole_contract_partial
from signal_engine.runtime.engine import BotRuntime, trading_day

NY = "America/New_York"


def _t(s):
    return pd.Timestamp(s, tz=NY)


def test_partials_are_whole_contracts():
    assert whole_contract_partial(0.5, 5) == pytest.approx(0.6)      # 3 closed, 2 left
    assert whole_contract_partial(0.5, 2) == pytest.approx(0.5)
    assert whole_contract_partial(0.1, 5) == pytest.approx(0.2)      # always close one
    assert whole_contract_partial(0.9, 5) == pytest.approx(0.8)      # always leave one
    assert whole_contract_partial(0.5, 1) == 0.5                     # single contract: as set
    assert whole_contract_partial(0.0, 5) == 0.0
    g = EngineConfig.load(strategy="zones").ghost.model_copy(update={"partial_at_tp1": 0.5})
    assert PaperTradeTracker.from_config(g).partial_at_tp1 == pytest.approx(0.6)


def test_live_strategy_is_one_3r_target_that_locks_in_halfway():
    cfg = EngineConfig.load(strategy="zones")
    z = cfg.setups.zone_retrace
    assert (z.fixed_target_r, z.single_target) == (3.0, True)
    # v6 (11-oct): halfway, 2 of 5 MNQ out and the stop of the rest to entry + 2
    assert cfg.ghost.partial_at_tp1 == 0.4 and cfg.ghost.breakeven_after_tp1 is True
    assert cfg.ghost.partial_at_progress == 0.5 and cfg.ghost.be_offset_points == 2.0
    assert cfg.ghost.be_at_progress is None


def test_trading_day_rolls_at_six_pm_like_apex():
    assert trading_day(_t("2026-09-27 18:30")) == "2026-09-28"       # Sunday open = Monday
    assert trading_day(_t("2026-09-28 16:59")) == "2026-09-28"
    assert trading_day(_t("2026-09-28 18:00")) == "2026-09-29"
    assert trading_day(pd.Timestamp("2026-09-28 10:00")) == "2026-09-28"   # naive = ET


def _runtime(max_trades=3, loss=500.0):
    cfg = EngineConfig.load(strategy="zones")
    cfg = cfg.model_copy(update={"ghost": cfg.ghost.model_copy(
        update={"max_trades_per_day": max_trades, "daily_loss_limit_usd": loss})})
    sent = []
    rt = SimpleNamespace(paper_tracker=PaperTradeTracker(), data_engine=SimpleNamespace(cfg=cfg),
                         output_dispatcher=SimpleNamespace(publish_day_done=sent.append),
                         _day_cache=None, _day_done_sent=None,
                         _point_values=lambda sym: {"5 MNQ": 10.0})
    rt._day_status = MethodType(BotRuntime._day_status, rt)
    rt._day_block = MethodType(BotRuntime._day_block, rt)
    return rt, sent


def _stopped_trade(rt, i, at, loss_pts):
    tr = rt.paper_tracker
    t = tr.open_trade(signal_id=f"s{i}", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=100.0, stop=100.0 - loss_pts, take_profits=[200.0], opened_at=at)
    tr.update_symbol("NQ", pd.Series({"high": 101.0, "low": 50.0, "close": 60.0}),
                     at + pd.Timedelta(minutes=5))
    assert not t.is_open


def test_three_trades_then_done_for_the_day():
    rt, sent = _runtime()
    day = _t("2026-09-28 10:00")
    for i in range(2):
        _stopped_trade(rt, i, day + pd.Timedelta(minutes=30 * i), 5)
    assert rt._day_block(day + pd.Timedelta(hours=2)) is None
    _stopped_trade(rt, 2, day + pd.Timedelta(hours=2), 5)
    why = rt._day_block(day + pd.Timedelta(hours=3))
    assert "3 trades" in why
    assert len(sent) == 1 and sent[0]["trades"] == 3 and sent[0]["pnl_usd"] == -150.0
    rt._day_block(day + pd.Timedelta(hours=4))
    assert len(sent) == 1                                   # told once per day
    assert rt._day_block(_t("2026-09-28 18:30")) is None    # new trading day


def test_loss_limit_in_dollars_on_the_position():
    rt, sent = _runtime()
    day = _t("2026-09-28 10:00")
    _stopped_trade(rt, 0, day, 30)                          # -30 pts = -$300 on 5 MNQ
    assert rt._day_block(day + pd.Timedelta(hours=1)) is None
    _stopped_trade(rt, 1, day + pd.Timedelta(hours=1), 25)  # -$550 total
    assert "pérdida máxima" in rt._day_block(day + pd.Timedelta(hours=2))


def test_limits_off_when_not_configured():
    rt, _ = _runtime(max_trades=None, loss=None)
    day = _t("2026-09-28 10:00")
    for i in range(5):
        _stopped_trade(rt, i, day + pd.Timedelta(minutes=20 * i), 30)
    assert rt._day_block(day + pd.Timedelta(hours=3)) is None


def test_live_strategy_has_no_daily_limits():
    cfg = EngineConfig.load(strategy="zones")
    g = cfg.ghost
    assert (g.max_trades_per_day, g.daily_loss_limit_usd) == (None, None)
    assert (cfg.risk.max_trades_per_day, cfg.risk.daily_loss_limit_dollar) == (None, None)


def test_messages_speak_in_contracts():
    tp1 = {"signal_id": "s", "instrument": "NQ", "entry": 100.0, "tp1": 120.0, "tp2": 160.0,
           "partial_fraction": 0.6, "position_label": "5 MNQ", "context": {"idea_number": 2}}
    e = build_discord_payload(OutputEvent.from_trade_tp1(tp1))["embeds"][0]
    assert e["description"].startswith("Cerré 3 MNQ · quedan 2")
    done = {"day": "2026-09-28", "trades": 3, "pnl_usd": -150.0,
            "reason": "ya se hicieron 3 trades hoy (límite 3)", "open_trade": False}
    e = build_discord_payload(OutputEvent.from_day_done(done))["embeds"][0]
    assert "Fin del día" in e["title"] and "−$150" in e["description"]


def _walk(tracker, direction, bars):
    t0 = _t("2026-09-28 10:00")
    long = direction == "LONG"
    t = tracker.open_trade(signal_id="b", instrument="NQ", direction=direction, setup="zone_retrace",
                           entry=100.0, stop=75.0 if long else 125.0,
                           take_profits=[175.0 if long else 25.0], opened_at=t0)
    for k, (h, l, c) in enumerate(bars):
        tracker.update_on_bar(t, pd.Series({"high": h, "low": l, "close": c}),
                              t0 + pd.Timedelta(minutes=k + 1))
        if not t.is_open:
            break
    return t


# these check where the stop moves, not what a fill costs
NO_COSTS = {"slippage_points": 0.0, "gap_fills": False,
            "partial_at_progress": None, "partial_at_tp1": 0.0, "breakeven_after_tp1": False}


def test_breakeven_plus_at_70_percent_of_the_way():
    # the mechanism still works when a strategy turns it on
    g = EngineConfig.load(strategy="zones").ghost.model_copy(
        update={"be_at_progress": 0.7, **NO_COSTS})
    # long: target 75 pts away, 70% = +52.5 pts. Runs +55 then falls through the stop
    t = _walk(PaperTradeTracker.from_config(g), "LONG", [(155, 120, 150), (152, 99, 101), (102, 70, 72)])
    assert t.exit_reason.value == "STOP" and t.pnl_points == 2.0      # entry + 2 pts, not -25
    # short, same shape
    t = _walk(PaperTradeTracker.from_config(g), "SHORT", [(101, 45, 50), (99, 48, 95), (130, 90, 128)])
    assert t.pnl_points == 2.0
    # only +50 pts (67%): the stop stays where it was
    t = _walk(PaperTradeTracker.from_config(g), "LONG", [(150, 120, 148), (149, 70, 72)])
    assert t.pnl_points == -25.0


def test_profit_lock_steps():
    # entry 100, stop 25 pts, target 75 pts away; steps 50% BE+2 · 70% keep 30% · 90% keep 60%
    g = EngineConfig.load(strategy="zones").ghost.model_copy(
        update={"lock_steps": [[0.5, 0.0], [0.7, 0.3], [0.9, 0.6]], "be_offset_points": 2.0,
                **NO_COSTS})
    # runs to +70 (93%), falls back: out at entry + 60% of 75 + 2 = +47
    t = _walk(PaperTradeTracker.from_config(g), "LONG", [(170, 120, 165), (166, 140, 141)])
    assert t.exit_reason.value == "STOP" and t.pnl_points == 47.0
    t = _walk(PaperTradeTracker.from_config(g), "SHORT", [(80, 30, 35), (60, 34, 59)])
    assert t.pnl_points == 47.0
    # only +54 (72%): 30% of the way + 2 = +24.5
    t = _walk(PaperTradeTracker.from_config(g), "LONG", [(154, 120, 150), (151, 110, 112)])
    assert t.pnl_points == 24.5
    # +40 (53%): BE+2
    t = _walk(PaperTradeTracker.from_config(g), "LONG", [(140, 120, 138), (139, 90, 92)])
    assert t.pnl_points == 2.0
