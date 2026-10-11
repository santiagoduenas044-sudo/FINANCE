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
    assert e["title"] == "🔔 Cierre del día · 29-Sep" and "Set up 1 · FVG 15" in e["description"]
    assert e["footer"]["text"].startswith("Trading simulado en papel")


def test_record_command_on_discord():
    rt = _runtime(_tracker(), [])
    out = build_reply("!record", rt)
    assert out.startswith("Cada set up por separado")
    assert "principales" not in out and "GANARON" not in out          # never added together
    assert "**Set up 1 · FVG 15**" in out and "**Set up 2 · APERTURA**" in out
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
    """The 16:55 message used to add the old 1-minute trades to FVG 15's
    record. Now today / week / total are the current version's, the older
    versions get one line apart, and the parallel variants one line each."""
    from signal_engine.output.record import variant_lines
    from signal_engine.versions import ghost_version_of
    tr = _tracker()                                   # untagged 1-min-era trades: v2 / v3
    tr.closed_trades[-1].context["bot_version"] = "v4"   # the BE+2 one: FVG 15
    shadow = SimpleNamespace(paper_tracker=_tracker())
    spec = SimpleNamespace(name="FVG 1 DÍA · zonas 1 min de día")
    now = _t("2026-09-29 16:56")
    text = build_record(tr.closed_trades, now, 10.0, "5 MNQ", version_of=ghost_version_of,
                        current="v4", title="GHOST v4 FVG 15",
                        extras=variant_lines([(spec, shadow)], now, 10.0, "5 MNQ"))
    assert text.startswith("📒 **GHOST v4 FVG 15** · 5 MNQ")
    assert "Hoy 29-Sep:** 1 trade · 1 ✅" in text and "perdido $0" in text and "**neto +$20**" in text
    assert "Total desde 29-Sep:** 1 trade · gana 100%" in text and "peor caída" not in text
    assert "_Versión anterior v2, aparte:_ 1 trade" in text          # each old version on its own
    assert "_Versión anterior v3, aparte:_ 1 trade" in text and "v2, v3" not in text
    assert "∥ **FVG 1 DÍA**: hoy 2 trades −$230 · total 3 trades +$520" in text
    last = text.split("**Últimos trades · GHOST v4 FVG 15**")[1]
    assert last.count("\n") == 1                     # only FVG 15's trade is listed


def test_daily_record_uses_the_current_version(tmp_path):
    sent = []
    tr = _tracker()
    rt = _runtime(tr, sent)
    rt._maybe_daily_record("NQ", _t("2026-09-29 16:57"))
    # its trades are all from the 1-minute versions: none counts as Set up 1
    assert "➖ **Set up 1 · FVG 15** — hoy sin trades" in sent[0][1]


def test_day_message_says_plainly_who_won_and_who_lost():
    from signal_engine.output.record import day_message
    T = "2026-10-05T{}:00-04:00"
    log = {"sources": [
        {"id": "ghost", "bot": "GHOST", "kind": "main", "setup": 1, "current": "v4",
         "setup_label": "Set up 1 · FVG 15", "position": "5 MNQ"},
        {"id": "ghost:zonas_1m", "bot": "GHOST", "kind": "variant", "setup": 3,
         "name": "Set up 3 · FVG 1 DÍA", "enabled": True},
        {"id": "ghost:setups_clasicos", "bot": "GHOST", "kind": "variant", "setup": 8,
         "name": "Set up 8 · CLÁSICO", "enabled": False},
        {"id": "orbit", "bot": "ORBIT", "kind": "main", "setup": 2, "current": "v3",
         "setup_label": "Set up 2 · APERTURA", "position": "5 MNQ"},
        {"id": "orbit:apertura_30m", "bot": "ORBIT", "kind": "variant", "setup": 6,
         "name": "Set up 6 · APERTURA 30", "enabled": True}],
        "trades": [
        {"src": "ghost", "version": "v2", "day": "2026-10-05", "net_usd": 999.0, "pts": 99,
         "closed_at": T.format("10:00")},                           # old version: not Set up 1
        {"src": "ghost", "version": "v4", "day": "2026-10-02", "net_usd": -355.2, "pts": -35,
         "closed_at": "2026-10-02T09:56:00-04:00"},
        {"src": "ghost", "version": "v4", "day": "2026-10-05", "net_usd": 289.8, "pts": 29.5,
         "side": "LONG", "entry": 31313.5, "exit": 31343.0, "why": "EOD",
         "closed_at": T.format("16:55")},
        {"src": "ghost:zonas_1m", "version": "v2", "day": "2026-10-05", "net_usd": -120.0,
         "pts": -12, "closed_at": T.format("11:00")},
        {"src": "orbit:apertura_30m", "version": "paralelo", "day": "2026-10-05",
         "net_usd": 400.0, "pts": 40, "closed_at": T.format("11:30")}]}
    text = day_message(log, pd.Timestamp("2026-10-05 17:00", tz="America/New_York"))
    assert text.startswith("Cada set up por separado")
    assert "principales" not in text and "GANARON" not in text and "Mejor de hoy" not in text
    lines = text.splitlines()
    assert len(lines) == 2 + 5                                         # one line per set up
    assert "✅ **Set up 1 · FVG 15** — hoy **GANÓ +$290** (1 trade) · total −$65" in lines  # v4 only
    assert lines.index("➖ **Set up 2 · APERTURA** — hoy sin trades") == 3   # number order
    assert "🛑 **Set up 3 · FVG 1 DÍA** — hoy **PERDIÓ −$120** (1 trade) · total −$120" in lines
    assert "⚪ Set up 8 · CLÁSICO — apagado" in lines
    assert "✅ **Set up 6 · APERTURA 30** — hoy **GANÓ +$400**" in text
