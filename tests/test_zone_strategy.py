"""Zone-retrace strategy: zone judgement, trade plan, idea lifecycle, messages."""
import pandas as pd

from signal_engine.config.schema import EngineConfig, ZoneRetraceParams
from signal_engine.features.market_structure.models import Direction
from signal_engine.output.discord import build_discord_payload
from signal_engine.output.events import OutputEvent
from signal_engine.runtime.ideas import IdeaBoard
from signal_engine.setups.zone_retrace import (
    ARMED, CANCELLED, CONFIRMED, FRESH, Zone, ZoneState, find_zones, judge_zone,
)

P = ZoneRetraceParams(enabled=True)
T0 = pd.Timestamp("2026-06-10 10:00", tz="America/New_York")


def _bars(rows, start=T0, freq="1min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


# A bullish 5m FVG: candle-1 high 100, candle-3 low 110 -> zone 100-110, mid 105.
ZONE = Zone("z1", Direction.BULLISH, T0, top=110.0, bottom=100.0, stop=85.0)


def test_find_zones_marks_a_bullish_gap_with_its_stop():
    b = _bars([(95, 100, 94, 99), (99, 118, 98, 117), (117, 125, 110, 124)], freq="5min")
    (z,) = find_zones(b, "NQ", P)
    assert (z.direction, z.bottom, z.top) == (Direction.BULLISH, 100.0, 110.0)
    assert z.stop == 105.0 - 20.0          # move origin only 12 pts away -> 20 floor
    # A bearish gap left by a move that started far above: stop capped at 35.
    b = _bars([(200, 250, 190, 192), (192, 193, 170, 171), (171, 180, 165, 166)],
              freq="5min")
    (z,) = find_zones(b, "NQ", P)
    assert (z.direction, z.bottom, z.top, z.stop) == (Direction.BEARISH, 180, 190, 185 + 35)


def test_zone_armed_confirmed_and_cancelled():
    t = T0 + pd.Timedelta(minutes=1)
    far = _bars([(160, 161, 150, 158)], t)
    near = _bars([(130, 131, 125, 128)], t)
    assert judge_zone(ZONE, far, P).state == FRESH          # 40 pts away
    assert judge_zone(ZONE, near, P).state == ARMED         # 15 pts away
    react = _bars([(120, 121, 112, 113), (112, 113, 103, 104), (104, 109, 102, 108)], t)
    st = judge_zone(ZONE, react, P)
    assert st.state == CONFIRMED and st.at == react.index[-1]
    through = _bars([(120, 121, 112, 113), (112, 113, 95, 96)], t)
    assert judge_zone(ZONE, through, P).state == CANCELLED


def test_detector_trade_plan_from_the_zone():
    from signal_engine.data.context import AnalysisContext, TimeframeView
    from signal_engine.features.analyzer import MarketAnalysis
    from signal_engine.setups.zone_retrace import ZoneRetraceDetector

    z5 = _bars([(95, 100, 94, 99), (99, 118, 98, 117), (117, 125, 110, 124)],
               T0 - pd.Timedelta(minutes=15), "5min")
    one = _bars([(120, 121, 112, 113), (112, 113, 103, 104), (104, 109, 102, 108)],
                z5.index[-1] + pd.Timedelta(minutes=1))
    ctx = AnalysisContext("NQ", one.index[-1], "RTH", htf=TimeframeView("1H", z5.iloc[:0]),
                          structure=TimeframeView("5m", z5), execution=TimeframeView("1m", one))
    (c,) = ZoneRetraceDetector(P).detect(ctx, MarketAnalysis("NQ", one.index[-1], {}))
    # Fill at the confirmation close; plan anchored at the midpoint 105, R=20.
    assert c.entry == 108 and c.stop_loss == 85.0
    assert c.take_profits == [105 + 20, 105 + 3 * 20]         # 1R, fallback 3R
    assert c.market_context["plan_rr"] == 3.0 and c.market_context["zone_id"]


def test_idea_board_heads_up_cancel_direct_and_numbering(tmp_path):
    path = tmp_path / "ideas.json"
    board = IdeaBoard(path)
    armed = ZoneState(ZONE, ARMED, None, 15.0)
    (ev,) = board.review("NQ", [armed], T0, window_open=True, can_announce=True)
    assert (ev["event"], ev["number"], ev["mid"], ev["risk_points"]) == ("idea", 1, 105.0, 20.0)
    assert board.review("NQ", [armed], T0, window_open=True, can_announce=True) == []

    (ev,) = board.review("NQ", [ZoneState(ZONE, CANCELLED, T0)], T0,
                         window_open=True, can_announce=True)
    assert ev["event"] == "cancelled" and "sin la reacción" in ev["reason"]

    other = Zone("z2", Direction.BEARISH, T0, top=200, bottom=190, stop=215)
    idea = IdeaBoard(path).claim_for_trade("NQ", ZoneState(other, CONFIRMED, T0), "z2", T0)
    assert (idea.number, idea.direct) == (2, True)     # numbering survived a reload


def test_messages_read_like_trade_ideas():
    idea = {"event": "idea", "number": 3, "symbol": "NQ", "direction": "LONG",
            "mid": 21450.25, "top": 21460.5, "bottom": 21440.0, "stop": 21430.25,
            "risk_points": 20.0, "zone_id": "z", "risk_usd_micro": 40.0}
    e = build_discord_payload(OutputEvent.from_trade_idea(idea))["embeds"][0]
    assert e["title"] == "🟡 TRADE IDEA #3 — posible COMPRA · NQ"
    assert "21,450.25" in e["description"] and "NO entres todavía" in e["description"]
    assert "$40.00 por micro" in e["description"]

    trade = {"signal_id": "s", "instrument": "NQ", "direction": "SHORT", "entry": 100.0,
             "stop": 120.0, "tp1": 75.0, "tp2": 40.0, "pnl_points": -20.0,
             "r_multiple": -1.0, "exit_reason": "STOP", "exit_price": 120.0,
             "context": {"idea_number": 4, "idea_direct": True, "zone_bottom": 95.0,
                         "zone_top": 105.0, "zone_mid": 100.0, "plan_rr": 3.0}}
    opened = build_discord_payload(OutputEvent.from_trade_opened(trade))["embeds"][0]
    assert opened["title"] == "⚡ SEÑAL DIRECTA #4 — VENTA · NQ"
    assert "óptima **100.00**" in opened["description"] and "(R:R 3)" in opened["description"]
    assert "entra ahora" in opened["description"]
    closed = build_discord_payload(OutputEvent.from_trade_closed(trade))["embeds"][0]
    assert closed["title"] == "🛑 IDEA #4 — Stop · -20.00 pts"


def test_live_bot_runs_the_zone_strategy_by_default(monkeypatch):
    monkeypatch.delenv("BOT_STRATEGY", raising=False)
    cfg = EngineConfig.load_active()
    assert cfg.setups.zone_retrace.enabled and not cfg.setups.orb.enabled
    assert cfg.timeframe_roles.execution == "1m" and cfg.strategy == "zones"
    monkeypatch.setenv("BOT_STRATEGY", "legacy")
    assert not EngineConfig.load_active().setups.zone_retrace.enabled
