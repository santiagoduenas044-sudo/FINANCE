"""The record in dollars on Discord: !record and the daily message after the flat."""
import re
from types import MethodType, SimpleNamespace

import pandas as pd

from signal_engine.config.schema import EngineConfig
from signal_engine.output.discord import build_discord_payload
from signal_engine.output.discord_bot import build_reply
from signal_engine.output.events import OutputEvent
from signal_engine.output.record import build_record
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.runtime.engine import BotRuntime

NY = "America/New_York"


def _t(s):
    return pd.Timestamp(s, tz=NY)


def _tracker():
    tr = PaperTradeTracker()
    # (opened, direction, entry, stop, target, bar that closes it)
    for i, (opened, d, e, sl, tp, bar) in enumerate([
        ("2026-09-28 10:00", "SHORT", 100.0, 125.0, 25.0, (101, 24, 30)),     # objetivo +75
        ("2026-09-29 10:00", "LONG", 100.0, 75.0, 175.0, (101, 70, 72)),      # stop -25
        ("2026-09-29 11:00", "LONG", 100.0, 102.0, 175.0, (103, 101, 102)),   # BE+2 (stop above entry)
    ]):
        t = tr.open_trade(signal_id=f"s{i}", instrument="NQ", direction=d, setup="zone_retrace",
                          entry=e, stop=sl if not (d == "LONG" and sl > e) else 75.0,
                          take_profits=[tp], opened_at=_t(opened), context={"idea_number": i + 1})
        if d == "LONG" and sl > e:
            t.stop = sl                                   # moved to entry + 2
        h, l, c = bar
        tr.update_symbol("NQ", pd.Series({"high": h, "low": l, "close": c}),
                         _t(opened) + pd.Timedelta(minutes=5))
    return tr


def test_record_reads_in_dollars():
    text = build_record(_tracker().closed_trades, _t("2026-09-29 16:56"), 10.0, "5 MNQ")
    assert ("Hoy 29-Sep:** 2 trades · 1 ✅ 0 ➖ 1 🛑 · ganado +$20 · perdido −$250 · "
            "**neto −$230**") in text
    assert "Esta semana:** 3 trades" in text and "**neto +$520**" in text
    assert "BE+ · **+$20**" in text and "objetivo · **+$750**" in text
    assert not re.search(r"\d(\.\d+)?R\b", text)            # no multiples of the stop


def _runtime(tracker, sent):
    cfg = EngineConfig.load(strategy="zones")
    rt = SimpleNamespace(paper_tracker=tracker, data_engine=SimpleNamespace(cfg=cfg),
                         output_dispatcher=SimpleNamespace(
                             publish_daily_record=lambda text, day: sent.append((day, text))),
                         _record_sent=None, _point_values=lambda s: {"5 MNQ": 10.0},
                         live_feeds={"NQ": None})
    rt._maybe_daily_record = MethodType(BotRuntime._maybe_daily_record, rt)
    return rt


def test_daily_record_once_after_the_flat():
    sent = []
    rt = _runtime(_tracker(), sent)
    rt._maybe_daily_record("NQ", _t("2026-09-29 15:00"))
    assert sent == []                                         # not before 16:55
    rt._maybe_daily_record("NQ", _t("2026-09-29 16:55"))
    rt._maybe_daily_record("NQ", _t("2026-09-29 16:58"))
    assert len(sent) == 1 and sent[0][0] == "2026-09-29"
    rt._maybe_daily_record("NQ", _t("2026-09-30 16:56"))
    assert len(sent) == 2                                     # next day, again once
    e = build_discord_payload(OutputEvent.from_daily_record(sent[0][1], sent[0][0]))["embeds"][0]
    assert e["title"] == "🔔 Cierre del día · 29-Sep" and "Set up 1 · IMÁN 15" in e["description"]
    assert e["footer"]["text"].startswith("Trading simulado en papel")


def test_record_command_on_discord():
    rt = _runtime(_tracker(), [])
    out = build_reply("!record", rt)
    assert out.startswith("📒 **Set up 1 · IMÁN 15** · GHOST v4 · 5 MNQ ($10 por punto")
    assert build_reply("!récord", rt) == out
    trades = build_reply("!trades", rt)
    assert "$" in trades and not re.search(r"\d(\.\d+)?R\b", trades)


def test_record_shows_net_after_commissions():
    from signal_engine.output.record import after_fees, commission_per_trade
    assert commission_per_trade("5 MNQ") == 5 * 1.04
    assert commission_per_trade("1 NQ") == 3.10                  # Tradovate: $1.55 per side
    assert commission_per_trade("1 punto NQ") == 0.0
    # 3 trades, +12 pts on 5 MNQ = +$120 gross; 3 x $5.20 = $15.60 -> +$104
    assert after_fees([10.0, -4.0, 6.0], 10.0, 5.2) == " · con comisiones +$104"
    assert after_fees([], 10.0, 5.2) == ""


def test_day_message_counts_only_the_version_running_now():
    """The 16:55 message used to add the old 1-minute trades to IMÁN 15's
    record. Now today / week / total are the current version's, the older
    versions get one line apart, and the parallel variants one line each."""
    from signal_engine.output.record import variant_lines
    from signal_engine.versions import ghost_version_of
    tr = _tracker()                                   # untagged 1-min-era trades: v2 / v3
    tr.closed_trades[-1].context["bot_version"] = "v4"   # the BE+2 one: IMÁN 15
    shadow = SimpleNamespace(paper_tracker=_tracker())
    spec = SimpleNamespace(name="IMÁN 1 · zonas 1 min de día")
    now = _t("2026-09-29 16:56")
    text = build_record(tr.closed_trades, now, 10.0, "5 MNQ", version_of=ghost_version_of,
                        current="v4", title="GHOST v4 IMÁN 15",
                        extras=variant_lines([(spec, shadow)], now, 10.0, "5 MNQ"))
    assert text.startswith("📒 **GHOST v4 IMÁN 15** · 5 MNQ")
    assert "Hoy 29-Sep:** 1 trade · 1 ✅" in text and "perdido $0" in text and "**neto +$20**" in text
    assert "Total desde 29-Sep:** 1 trade · gana 100%" in text and "peor caída" not in text
    assert "_Versiones anteriores (v2, v3), aparte:_ 2 trades · neto +$500" in text
    assert "∥ **IMÁN 1**: hoy 2 trades −$230 · total 3 trades +$520" in text
    last = text.split("**Últimos trades · GHOST v4 IMÁN 15**")[1]
    assert last.count("\n") == 1                     # only IMÁN 15's trade is listed


def test_daily_record_uses_the_current_version(tmp_path):
    sent = []
    tr = _tracker()
    rt = _runtime(tr, sent)
    rt._maybe_daily_record("NQ", _t("2026-09-29 16:57"))
    assert "**Set up 1 · IMÁN 15** · GHOST v4" in sent[0][1]
    assert "_Antes, GHOST con otras versiones (v2, v3):_" in sent[0][1]


def test_every_set_up_in_the_day_message():
    from signal_engine.output.record import setup_lines
    rows = [
        {"setup": 1, "label": "Set up 1 · IMÁN 15", "kind": "main", "on": True, "fee": 5.2,
         "today": {"n": 1, "net_usd": 295.0}, "total": {"n": 4, "net_usd": 645.0}},
        {"setup": 2, "label": "Set up 2 · APERTURA", "kind": "main", "on": True, "fee": 0.0,
         "today": {"n": 0, "net_usd": 0.0}, "total": {"n": 3, "net_usd": 55.0}},
        {"setup": 4, "label": "Set up 4 · IMÁN 1 NOCHE", "kind": "variant", "on": True,
         "fee": 5.2, "today": {"n": 3, "net_usd": 1985.0}, "total": {"n": 24, "net_usd": 1340.0}},
        {"setup": 6, "label": "Set up 6 · APERTURA 30", "kind": "variant", "on": True,
         "fee": 0.0, "today": {"n": 1, "net_usd": -120.0}, "total": {"n": 1, "net_usd": -120.0}},
        {"setup": 8, "label": "Set up 8 · CLÁSICO", "kind": "variant", "on": False, "fee": 5.2,
         "today": {"n": 0}, "total": {"n": 0}},
    ]
    lines = setup_lines(rows)
    assert lines[0].startswith("📋 **Todos los set ups**")
    assert "✅ **Set up 1 · IMÁN 15**: hoy 1 trade +$290 · total 4 trades +$624" in lines
    assert "➖ **Set up 2 · APERTURA**: hoy sin trades · total 3 trades +$55" in lines
    assert "🛑 **Set up 6 · APERTURA 30**: hoy 1 trade −$120 · total 1 trade −$120" in lines
    assert "⚪ Set up 8 · CLÁSICO: apagado · sin trades todavía" in lines
    assert lines[-1] == "🏆 Mejor de hoy: **Set up 4 · IMÁN 1 NOCHE** +$1,969"
