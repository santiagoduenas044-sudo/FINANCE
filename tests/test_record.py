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
    assert "Hoy 29-Sep:** 2 trades · 1 ✅ 0 ➖ 1 🛑 · **−$230**" in text
    assert "Esta semana:** 3 trades" in text and "**+$520**" in text
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
    assert "Cierre del día" in e["title"] and "Récord" in e["description"]


def test_record_command_on_discord():
    rt = _runtime(_tracker(), [])
    out = build_reply("!record", rt)
    assert out.startswith("📒 **Récord · 5 MNQ**")
    assert build_reply("!récord", rt) == out
    trades = build_reply("!trades", rt)
    assert "$" in trades and not re.search(r"\d(\.\d+)?R\b", trades)
