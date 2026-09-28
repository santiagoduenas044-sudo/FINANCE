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


def test_live_strategy_is_one_3r_target_without_breakeven():
    cfg = EngineConfig.load(strategy="zones")
    z = cfg.setups.zone_retrace
    assert (z.fixed_target_r, z.single_target) == (3.0, True)
    assert cfg.ghost.partial_at_tp1 == 0.0 and cfg.ghost.breakeven_after_tp1 is False


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


def test_live_strategy_has_daily_limits():
    g = EngineConfig.load(strategy="zones").ghost
    assert (g.max_trades_per_day, g.daily_loss_limit_usd) == (3, 500)


def test_messages_speak_in_contracts():
    tp1 = {"signal_id": "s", "instrument": "NQ", "entry": 100.0, "tp1": 120.0, "tp2": 160.0,
           "partial_fraction": 0.6, "position_label": "5 MNQ", "context": {"idea_number": 2}}
    e = build_discord_payload(OutputEvent.from_trade_tp1(tp1))["embeds"][0]
    assert e["description"].startswith("Cerré 3 MNQ · quedan 2")
    done = {"day": "2026-09-28", "trades": 3, "pnl_usd": -150.0,
            "reason": "ya se hicieron 3 trades hoy (límite 3)", "open_trade": False}
    e = build_discord_payload(OutputEvent.from_day_done(done))["embeds"][0]
    assert "Fin del día" in e["title"] and "−$150" in e["description"]
