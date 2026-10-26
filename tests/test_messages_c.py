"""Discord messages, option C (26-oct): why + plan, the trade's story, the
set up's record, and the day's close with a chart and the pairs."""
import pandas as pd

from signal_engine.output import trade_text as tt
from signal_engine.output.discord import build_discord_payload
from signal_engine.output.events import OutputEvent
from signal_engine.output.record import day_summary
from signal_engine.paper.tracker import PaperTradeTracker, whole_contract_partial
from signal_engine.runtime.shadows import alert_note

NY = "America/New_York"
T0 = pd.Timestamp("2026-10-27 04:12", tz=NY)
PV = {"5 MNQ": 10.0}
RULES = {"partial": 0.4, "be_at": None, "time_exit": None, "eod": "16:55",
         "be_offset": 2.0, "lock_after_partial": True}


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def _trade():
    tr = PaperTradeTracker(partial_at_tp1=whole_contract_partial(0.4, 5), breakeven_after_tp1=True,
                           be_offset_points=2.0, partial_at_progress=0.5)
    t = tr.open_trade(signal_id="x", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=25072.0, stop=25037.0, take_profits=[25177.0], opened_at=T0, timeframe="15m",
                      context={"zone_top": 25060, "zone_bottom": 25040, "zone_mid": 25050,
                               "plan_rules": RULES})
    return tr, t


def test_entry_says_why_and_the_plan():
    _tr, t = _trade()
    title, text, _ = alert_note("Set up 5 · FVG 15 CALMA", "open", t, PV, [])
    assert title == "🟢 Set up 5 · FVG 15 CALMA — COMPRA NQ en 25,072"
    assert "Primer regreso a la zona FVG de 15 min (25,040 – 25,060)" in text
    assert "1. Stop 25,037 (−$350)" in text
    assert "2. En 25,124.5: cierra 2 MNQ (+$210) y el stop sube a 25,074" in text
    assert "3. Objetivo 25,177 (+$1,050) o cierre a las 16:55" in text


def test_exit_tells_the_story_with_times():
    tr, t = _trade()
    tr.update_symbol("NQ", _bar(25075, 25130, 25070, 25120), T0 + pd.Timedelta(minutes=43))
    tr.update_symbol("NQ", _bar(25120, 25180, 25110, 25170), T0 + pd.Timedelta(minutes=84))
    title, text, _ = alert_note("Set up 5 · FVG 15 CALMA", "close", t, PV, tr.closed_trades)
    assert title == "✅ Set up 5 · FVG 15 CALMA — GANÓ +$840"
    assert "`04:12` Compra en 25,072" in text
    assert "`04:55` 💰 Mitad 25,124.5: cierra 2 MNQ +$210 · stop a 25,074" in text
    assert "`05:36` 🎯 Objetivo 25,177: cierra 3 MNQ +$630" in text
    assert "Set up 5: 1 trade · 100 % · +$835" in text                # after commissions


def test_a_stop_reads_as_a_loss():
    tr, t = _trade()
    tr.update_symbol("NQ", _bar(25070, 25074, 25030, 25035), T0 + pd.Timedelta(minutes=5))
    title, text, color = alert_note("Set up 5 · FVG 15 CALMA", "close", t, PV, tr.closed_trades)
    assert title.startswith("🛑") and "PERDIÓ −$350" in title and color == 0xE74C3C
    assert "🛑 Stop 25,037" in text


def test_set_up_1_alert_and_entry_use_the_same_texts():
    ev = {"event": "idea", "number": 12, "symbol": "NQ", "direction": "LONG", "zone_id": "z",
          "bottom": 25040, "top": 25060, "mid": 25050, "stop": 25015, "setup_tag": "Set up 1 · FVG 15",
          "usd_per_point": PV, "record": "Set up 1: 3 trades · 67 % · +$420",
          "plan": {"final": 25155, "distance": 18}}
    e = build_discord_payload(OutputEvent.from_trade_idea(ev))["embeds"][0]
    assert e["title"] == "🟡 Set up 1 · FVG 15 · IDEA #12 — Zona de COMPRA en NQ · a 18 pts"
    assert "compro ~25,050 · stop 25,015 (−$350) · objetivo 25,155 (+$1,050)" in e["description"]
    assert "Idea #12 · Set up 1: 3 trades" in e["description"]
    _tr, t = _trade()
    td = {**t.to_dict(), "setup_tag": "Set up 1 · FVG 15", "record": "Set up 1: 3 trades · 67 % · +$420"}
    td["context"] = {**td["context"], "idea_number": 12, "usd_per_point": PV}
    e = build_discord_payload(OutputEvent.from_trade_opened(td))["embeds"][0]
    assert e["title"].endswith("COMPRA NQ en 25,072") and "**El plan**" in e["description"]


def test_record_line():
    assert tt.record_line("Set up 9", [], 10.0) == "Set up 9: todavía sin trades cerrados"
    assert tt.record_line("Set up 9", [10, -5], 10.0, 5.2) == "Set up 9: 2 trades · 50 % · +$40"


def test_day_summary_has_bars_counts_and_pairs():
    log = {"sources": [
        {"id": "ghost", "setup": 1, "setup_label": "Set up 1 · FVG 15", "kind": "main", "current": "v8"},
        {"id": "ghost:a", "setup": 5, "name": "Set up 5 · FVG 15 CALMA", "kind": "variant", "enabled": True},
        {"id": "ghost:b", "setup": 16, "name": "Set up 16 · FVG 15 FILTROS", "kind": "variant",
         "enabled": True, "vs": 1},
        {"id": "ghost:c", "setup": 4, "name": "Set up 4 · FVG 1", "kind": "variant", "retired": True}],
        "trades": [
        {"src": "ghost", "version": "v7", "day": "2026-10-20", "net_usd": 999.0},     # older version
        {"src": "ghost", "version": "v8", "day": "2026-10-27", "net_usd": -355.2},
        {"src": "ghost:a", "day": "2026-10-27", "net_usd": 835.0},
        {"src": "ghost:b", "day": "2026-10-27", "net_usd": 0.0},
        {"src": "ghost:c", "day": "2026-10-27", "net_usd": -999.0}]}
    text, png = day_summary(log, "2026-10-27 17:00")
    assert text.startswith("**1 ganó · 1 perdió · 0 sin trades**")
    assert "Mejor: 5 FVG 15 CALMA +$835" in text and "Peor: 1 FVG 15 −$355" in text
    assert "16 FVG 15 FILTROS vs 1 → +$355 mejor" in text
    assert "FVG 1 " not in text.replace("FVG 15", "")                     # retired: not listed
    assert png is None or png.startswith(b"\x89PNG")
    text, png = day_summary({"sources": log["sources"], "trades": []}, "2026-10-27 17:00")
    assert "ningún set up operó" in text and png is None
