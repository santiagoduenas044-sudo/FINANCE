"""A trade nobody watched (the bot or its set up was off) closes where the bot
last saw it, never at a price days later; old records of that kind are voided."""
import json

import pandas as pd

from signal_engine.paper.models import ExitReason, TradeStatus
from signal_engine.paper.tracker import PaperTradeTracker

NY = "America/New_York"


def _t(s):
    return pd.Timestamp(s, tz=NY)


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def _open(tr, at="2026-10-10 10:00"):
    t = tr.open_trade(signal_id="s", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=25000.0, stop=24970.0, take_profits=[25090.0], opened_at=_t(at))
    tr.update_symbol("NQ", _bar(25000, 25012, 24995, 25010), _t(at) + pd.Timedelta(minutes=1))
    return t


def test_back_a_week_later_it_closes_at_the_last_price_seen(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    t = _open(tr)
    # nine days later NQ is 1,600 pts lower: not a -1,600 stop
    tr.update_symbol("NQ", _bar(23400, 23410, 23390, 23400), _t("2026-10-19 10:00"))
    assert not t.is_open and t.exit_price == 25010.0 and t.pnl_points == 10.0
    assert t.closed_at == _t("2026-10-10 10:01")
    assert "no estaba mirando" in t.context["close_note"]


def test_the_eod_close_after_a_week_also_uses_the_last_price_seen(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    t = _open(tr)
    tr.close_symbol_eod("NQ", 23400.0, _t("2026-10-19 20:00"))
    assert t.exit_price == 25010.0


def test_a_normal_bar_still_stops_the_trade(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    t = _open(tr)
    tr.update_symbol("NQ", _bar(25005, 25006, 24960, 24965), _t("2026-10-10 10:30"))
    assert not t.is_open and t.exit_reason.value == "STOP" and t.pnl_points == -30.0


def test_old_week_long_trades_leave_the_record(tmp_path):
    def row(sid, opened, closed, pnl):
        tr = PaperTradeTracker()
        t = tr.open_trade(signal_id=sid, instrument="NQ", direction="LONG", setup="zone_retrace",
                          entry=25000.0, stop=24970.0, take_profits=[25090.0],
                          opened_at=_t(opened))
        tr._close(t, _t(closed), 25000.0 + pnl, ExitReason.STOP, TradeStatus.STOPPED)
        return json.dumps(t.to_dict())
    (tmp_path / "paper_closed.jsonl").write_text("\n".join([
        row("ok", "2026-10-09 10:00", "2026-10-09 11:00", -30.0),
        row("night", "2026-10-08 19:00", "2026-10-09 09:00", 50.0),       # same trading day
        row("bad", "2026-10-10 10:00", "2026-10-19 09:31", -1600.0)]) + "\n")
    tr = PaperTradeTracker(store_dir=tmp_path)
    assert [t.signal_id for t in tr.closed_trades] == ["ok", "night"]
    voided = [json.loads(x) for x in (tmp_path / "paper_voided.jsonl").read_text().splitlines()]
    assert [v["signal_id"] for v in voided] == ["bad"] and voided[0]["void_reason"]
    assert list(tmp_path.glob("paper_closed.before-void-*.jsonl"))          # the copy
    # loading again changes nothing more
    tr2 = PaperTradeTracker(store_dir=tmp_path)
    assert [t.signal_id for t in tr2.closed_trades] == ["ok", "night"]
    assert len((tmp_path / "paper_voided.jsonl").read_text().splitlines()) == 1


def test_after_a_power_cut_the_stop_fills_at_its_level(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    t = _open(tr)
    # back 3 hours later the same day, NQ 1,500 pts lower: the stop was at the broker
    tr.update_symbol("NQ", _bar(23500, 23510, 23490, 23500), _t("2026-10-10 13:01"))
    assert t.exit_reason.value == "STOP" and t.exit_price == 24970.0


def test_a_live_gap_still_fills_at_the_open(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path, gap_fills=True)
    t = _open(tr)
    tr.update_symbol("NQ", _bar(24950, 24955, 24940, 24945), _t("2026-10-10 10:02"))
    assert t.exit_price == 24950.0                                  # one minute later: real gap


def test_a_loss_of_many_stops_leaves_the_record(tmp_path):
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="cut", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=25000.0, stop=24965.0, take_profits=[25105.0], opened_at=_t("2026-10-14 10:00"))
    tr._close(t, _t("2026-10-14 13:00"), 23500.0, ExitReason.STOP, TradeStatus.STOPPED)  # -1,500 pts
    (tmp_path / "paper_closed.jsonl").write_text(json.dumps(t.to_dict()) + "\n")
    assert PaperTradeTracker(store_dir=tmp_path).closed_trades == []
    v = json.loads((tmp_path / "paper_voided.jsonl").read_text())
    assert "3 veces su stop" in v["void_reason"]
