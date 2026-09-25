"""Zone-retrace strategy: zone judgement, trade plan, idea lifecycle, messages."""
import pytest
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
            "risk_points": 20.0, "zone_id": "z",
            "usd_per_point": {"NQ": 20.0, "MNQ": 2.0}}
    e = build_discord_payload(OutputEvent.from_trade_idea(idea))["embeds"][0]
    assert e["title"] == "🟡 IDEA #3 — posible COMPRA · NQ"
    assert "Vigila **21,450.25**" in e["description"] and "No entres aún" in e["description"]
    assert "~20 pts ≈ $40 MNQ / $400 NQ" in e["description"]

    trade = {"signal_id": "s", "instrument": "NQ", "direction": "SHORT", "entry": 100.0,
             "stop": 120.0, "tp1": 75.0, "tp2": 40.0, "pnl_points": -20.0,
             "r_multiple": -1.0, "exit_reason": "STOP", "exit_price": 120.0,
             "context": {"idea_number": 4, "idea_direct": True, "zone_bottom": 95.0,
                         "usd_per_point": {"NQ": 20.0, "MNQ": 2.0},
                         "zone_top": 105.0, "zone_mid": 100.0, "plan_rr": 3.0}}
    opened = build_discord_payload(OutputEvent.from_trade_opened(trade))["embeds"][0]
    assert opened["title"] == "⚡ SEÑAL DIRECTA #4 — VENTA · NQ"
    assert opened["description"].splitlines() == [
        "✅ **Entra ahora 100.00** · óptima 100.00",
        "🛑 SL 120.00 · ~20 pts ≈ $40 MNQ / $400 NQ",
        "🎯 TP1 75.00 → stop a BE · TP 40.00 (3R)"]
    closed = build_discord_payload(OutputEvent.from_trade_closed(trade))["embeds"][0]
    assert closed["title"] == "🛑 IDEA #4 — Stop · -20.00 pts"
    assert closed["description"] == "100.00 → 120.00 · -1.00R · −$40 MNQ · −$400 NQ"


def test_live_bot_runs_the_zone_strategy_by_default(monkeypatch):
    monkeypatch.delenv("BOT_STRATEGY", raising=False)
    cfg = EngineConfig.load_active()
    assert cfg.setups.zone_retrace.enabled and not cfg.setups.orb.enabled
    assert cfg.timeframe_roles.execution == "1m" and cfg.strategy == "zones"
    monkeypatch.setenv("BOT_STRATEGY", "legacy")
    assert not EngineConfig.load_active().setups.zone_retrace.enabled


def test_partial_at_tp1_banks_half_then_breakeven(tmp_path):
    from signal_engine.paper.tracker import PaperTradeTracker
    tr = PaperTradeTracker(breakeven_after_tp1=True, partial_at_tp1=0.5)
    t = tr.open_trade(signal_id="p", instrument="NQ", direction="SHORT", setup="zone_retrace",
                      entry=100.0, stop=135.0, take_profits=[65.0, -75.0], opened_at=T0)
    bar = lambda h, l, c: pd.Series({"open": c, "high": h, "low": l, "close": c})
    tr.update_on_bar(t, bar(101, 60, 70), T0 + pd.Timedelta(minutes=1))   # TP1: bank half
    assert t.is_open and t.stop == 100.0 and t.pnl_points == 0.5 * 35 + 0.5 * 30
    tr.update_on_bar(t, bar(105, 90, 104), T0 + pd.Timedelta(minutes=2))  # back to entry
    assert not t.is_open and t.pnl_points == 17.5                           # half of 35 kept
    closed = build_discord_payload(OutputEvent.from_trade_closed(
        {**t.to_dict(), "context": {"idea_number": 1}}))["embeds"][0]
    assert closed["title"] == "✅ IDEA #1 — BE tras parcial · +17.50 pts"


def test_heads_up_is_short_and_has_the_numbers():
    idea = {"event": "idea", "number": 2, "symbol": "NQ", "direction": "SHORT",
            "mid": 30841.5, "top": 30850.75, "bottom": 30832.25, "stop": 30876.5,
            "risk_points": 35.0, "zone_id": "z", "usd_per_point": {"NQ": 20.0, "MNQ": 2.0},
            "plan": {"tp1": 30806.5, "final": 30670.0, "plan_rr": 4.9}}
    e = build_discord_payload(OutputEvent.from_trade_idea(idea))["embeds"][0]
    assert e["title"] == "🟡 IDEA #2 — posible VENTA · NQ"
    assert e["description"].splitlines() == [
        "Vigila **30,841.50** (zona 30,832.25 – 30,850.75)",
        "🛑 SL 30,876.50 · ~35 pts ≈ $70 MNQ / $700 NQ",
        "🎯 TP1 30,806.50 · TP 30,670.00 (4.9R)",
        "⏳ **Todavía no estoy seguro** — esperemos la reacción en la zona. No entres aún."]


def _bar(h, l, c):
    return pd.Series({"open": c, "high": h, "low": l, "close": c})


def test_breakeven_trigger_and_trailing_only_tighten():
    from signal_engine.paper.tracker import PaperTradeTracker
    tr = PaperTradeTracker(be_trigger_r=0.5, trail_start_r=1.0, trail_distance_r=0.5)
    t = tr.open_trade(signal_id="tr", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=100.0, stop=80.0, take_profits=[200.0], opened_at=T0)
    m = lambda k: T0 + pd.Timedelta(minutes=k)
    tr.update_on_bar(t, _bar(111, 99, 110), m(1))      # +0.55R -> stop to entry
    assert t.stop == 100.0
    tr.update_on_bar(t, _bar(130, 115, 128), m(2))     # +1.5R -> trail 0.5R (10) behind 130
    assert t.stop == 120.0
    tr.update_on_bar(t, _bar(129, 121, 125), m(3))     # pullback: stop never loosens
    assert t.stop == 120.0 and t.is_open
    tr.update_on_bar(t, _bar(124, 118, 119), m(4))     # trailing stop hit
    assert not t.is_open and t.pnl_points == 20.0
    closed = build_discord_payload(OutputEvent.from_trade_closed(
        {**t.to_dict(), "context": {"idea_number": 7}}))["embeds"][0]
    assert closed["title"] == "✅ IDEA #7 — Trailing stop · +20.00 pts"


def test_stop_move_message_is_short():
    trade = {"signal_id": "x", "instrument": "NQ", "direction": "SHORT", "entry": 100.0,
             "stop": 90.0, "tp2": 40.0, "current_price": 80.0, "context": {"idea_number": 3}}
    e = build_discord_payload(OutputEvent.from_stop_moved(trade, 100.0))["embeds"][0]
    assert e["title"] == "🔒 IDEA #3 — trailing: stop a 90.00 · NQ"
    assert e["description"] == "Asegura +10.00 pts · precio 80.00 · resto a 40.00"


def test_heads_up_goes_out_with_its_chart():
    from signal_engine.config.schema import DiscordOutputConfig
    from signal_engine.output.discord import DiscordOutputAdapter
    sent = []
    adapter = DiscordOutputAdapter(
        DiscordOutputConfig(enabled=True), webhook_url="https://discord.invalid/x",
        transport=lambda *a: pytest.fail("a chart must go as a file upload"),
        file_transport=lambda url, payload, data, name, timeout: sent.append((payload, name)) or 200)
    idea = {"event": "idea", "number": 1, "symbol": "NQ", "direction": "LONG", "mid": 10.0,
            "top": 11.0, "bottom": 9.0, "stop": -10.0, "risk_points": 20.0, "zone_id": "z"}
    assert adapter.emit(OutputEvent.from_trade_idea(idea, image_png=b"\x89PNG"))
    payload, name = sent[0]
    assert payload["embeds"][0]["image"] == {"url": f"attachment://{name}"}


def test_plan_chart_renders():
    pytest.importorskip("matplotlib")
    from signal_engine.output.trade_chart import render_plan_png
    bars = pd.DataFrame({"open": [10.0, 11], "high": [12.0, 12], "low": [9.0, 10],
                         "close": [11.0, 10.5]},
                        index=pd.date_range(T0, periods=2, freq="1min"))
    png = render_plan_png(bars, direction="SHORT", zone_bottom=11, zone_top=12, entry=11.5,
                          stop=15, tp1=8, final=2, status="ESPERANDO")
    assert png[:4] == b"\x89PNG"


def test_fixed_two_to_one_single_target():
    from signal_engine.data.context import AnalysisContext, TimeframeView
    from signal_engine.features.analyzer import MarketAnalysis
    from signal_engine.setups.zone_retrace import ZoneRetraceDetector

    z5 = _bars([(95, 100, 94, 99), (99, 118, 98, 117), (117, 125, 110, 124)],
               T0 - pd.Timedelta(minutes=15), "5min")
    one = _bars([(120, 121, 112, 113), (112, 113, 103, 104), (104, 109, 102, 108)],
                z5.index[-1] + pd.Timedelta(minutes=1))
    ctx = AnalysisContext("NQ", one.index[-1], "RTH", htf=TimeframeView("1H", z5.iloc[:0]),
                          structure=TimeframeView("5m", z5), execution=TimeframeView("1m", one))
    p = ZoneRetraceParams(enabled=True, fixed_target_r=2.0, single_target=True)
    (c,) = ZoneRetraceDetector(p).detect(ctx, MarketAnalysis("NQ", one.index[-1], {}))
    assert c.take_profits == [105 + 2 * 20]                  # mid 105, R 20 -> 2:1


def test_dollars_are_for_five_micros():
    from signal_engine.data.contracts import ContractRegistry
    from signal_engine.runtime.engine import position_usd_per_point
    g = EngineConfig.load(strategy="zones").ghost
    assert position_usd_per_point(ContractRegistry.load(), g, "NQ") == {"5 MNQ": 10.0}
