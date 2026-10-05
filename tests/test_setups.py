"""Set ups: numbered short names, Discord alerts for every set up's entries and
exits, and the control table (dashboard + !setups)."""
import pandas as pd

from signal_engine.api.bots import setup_rows, setups_text
from signal_engine.output.discord import _build_idea_trade_opened_payload
from signal_engine.output.events import OutputEvent
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.runtime.shadows import (ShadowSpec, alert_note, load_specs,
                                           run_ghost_shadows, trade_events)
from signal_engine.subbots.orbit import setup_publisher
from signal_engine.versions import MAIN_SETUP, main_setup_label

T0 = pd.Timestamp("2026-10-05 10:00", tz="America/New_York")


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def test_every_set_up_has_its_own_number_and_a_short_name():
    specs = load_specs("ghost") + load_specs("orbit")
    nums = [sp.setup for sp in specs]
    assert all(nums) and len(set(nums)) == len(nums)
    assert not set(nums) & set(MAIN_SETUP.values())          # 1 and 2 are the bots
    for sp in specs:
        assert " · " not in sp.name                          # the code only
        assert sp.label == f"Set up {sp.setup} · {sp.name}"
    assert main_setup_label("GHOST") == "Set up 1 · IMÁN 15"
    assert main_setup_label("ORBIT") == "Set up 2 · APERTURA"


def test_trade_events_sees_an_entry_and_its_exit(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    before = ({t.signal_id for t in tr.open_trades}, len(tr.closed_trades))
    tr.open_trade(signal_id="a", instrument="NQ", direction="LONG", setup="zone_retrace",
                  entry=100.0, stop=80.0, take_profits=[160.0], opened_at=T0)
    assert [(k, t.signal_id) for k, t in trade_events(tr, *before)] == [("open", "a")]
    before = ({t.signal_id for t in tr.open_trades}, len(tr.closed_trades))
    tr.update_symbol("NQ", _bar(100, 101, 79, 80), T0 + pd.Timedelta(minutes=1))
    assert [(k, t.signal_id) for k, t in trade_events(tr, *before)] == [("close", "a")]


def test_a_trade_opened_and_closed_on_the_same_bar_gives_both(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    tr.open_trade(signal_id="b", instrument="NQ", direction="SHORT", setup="zone_retrace",
                  entry=100.0, stop=120.0, take_profits=[40.0], opened_at=T0)
    tr.update_symbol("NQ", _bar(100, 121, 99, 120), T0 + pd.Timedelta(minutes=1))
    assert [k for k, _ in trade_events(tr, set(), 0)] == ["open", "close"]


def test_alert_note_in_points_and_dollars(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    t = tr.open_trade(signal_id="c", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=20_000.0, stop=19_975.0, take_profits=[20_075.0], opened_at=T0)
    pv = {"5 MNQ": 10.0}
    title, text, _ = alert_note("Set up 3 · IMÁN 1", "open", t, pv)
    assert title == "🟢 Set up 3 · IMÁN 1 — COMPRA NQ"
    assert "25.0 pts, −$250" in text and "75.0 pts, +$750" in text and "5 MNQ" in text
    tr.update_symbol("NQ", _bar(20_000, 20_080, 19_990, 20_070), T0 + pd.Timedelta(minutes=1))
    title, text, _ = alert_note("Set up 3 · IMÁN 1", "close", t, pv, tr.closed_trades)
    assert title.startswith("✅ Set up 3 · IMÁN 1 — cerrado") and "+75.00 pts" in title
    assert "+$750" in text and "Récord Set up 3 · IMÁN 1: 1 trade" in text


class _FakeRuntime:
    """Stands in for a shadow BotRuntime: opens a trade on its first cycle."""

    def __init__(self, store):
        self.paper_tracker = PaperTradeTracker(store_dir=store)

        class _Cfg:
            class timeframe_roles:
                execution = "1m"
        self.data_engine = type("D", (), {"cfg": _Cfg})()

    def run_cycle(self, symbol, tf_bars):
        self.paper_tracker.open_trade(signal_id="s1", instrument=symbol, direction="LONG",
                                      setup="zone_retrace", entry=1.0, stop=0.0,
                                      take_profits=[3.0], opened_at=T0)


def test_run_ghost_shadows_tells_each_entry_once(tmp_path):
    spec = ShadowSpec(id="zonas_1m", name="IMÁN 1", setup=3)
    rt = _FakeRuntime(tmp_path)
    heard = []
    bars = {"1m": pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
                               index=[T0])}
    for _ in range(2):          # the same candle twice: run (and tell) only once
        run_ghost_shadows([(spec, rt)], "NQ", bars, T0,
                          notify=lambda sp, r, kind, t: heard.append((sp.label, kind, t.signal_id)))
    assert heard == [("Set up 3 · IMÁN 1", "open", "s1")]


def test_orbit_set_up_publisher_tells_trades_only_with_its_own_key():
    sent = []
    say = setup_publisher(lambda **kw: sent.append(kw), "apertura_30m")
    for key in ("box:2026-10-05", "open:APERTURA-2026-10-05", "be:APERTURA-2026-10-05",
                "close:APERTURA-2026-10-05", "none:2026-10-05"):
        say(title="t", text="x", color=1, key=key, image=None)
    assert [s["key"] for s in sent] == ["setup:apertura_30m:open:APERTURA-2026-10-05",
                                        "setup:apertura_30m:be:APERTURA-2026-10-05",
                                        "setup:apertura_30m:close:APERTURA-2026-10-05"]


def test_orbit_set_up_publisher_respects_the_off_switch(monkeypatch):
    monkeypatch.setenv("BOT_AVISOS_SETUPS", "off")
    assert setup_publisher(lambda **kw: None, "x") is None


def test_ghost_own_messages_carry_set_up_1():
    p = {"direction": "LONG", "instrument": "NQ", "entry": 1.0, "stop": 0.0, "tp1": 2.0,
         "tp2": 3.0, "signal_id": "z", "setup_tag": "Set up 1 · IMÁN 15",
         "context": {"idea_number": 7, "zone_mid": 1.0}}
    payload = _build_idea_trade_opened_payload(OutputEvent.from_trade_opened(p))
    assert payload["embeds"][0]["title"].startswith("🟢 Set up 1 · IMÁN 15 · IDEA #7")


def test_control_table_lists_every_set_up_in_order():
    rec = {"n": 2, "net_usd": 150.0}
    bots = [
        {"name": "GHOST", "setup": 1, "setup_label": "Set up 1 · IMÁN 15", "status": "ONLINE",
         "now": [{"text": "Busca huecos."}], "open_usd": None, "main": {"today": rec, "total": rec},
         "variants": [{"id": "zonas_1m", "name": "Set up 3 · IMÁN 1", "setup": 3, "enabled": False,
                       "now": "Apagado.", "open_usd": None, "today": {"n": 0}, "total": rec}]},
        {"name": "ORBIT", "setup": 2, "setup_label": "Set up 2 · APERTURA", "status": "ONLINE",
         "now": [{"text": "Caja lista."}], "open_usd": None, "today": {"n": 0}, "total": rec,
         "variants": []},
    ]
    rows = setup_rows(bots)
    assert [r["setup"] for r in rows] == [1, 2, 3]
    assert [r["on"] for r in rows] == [True, True, False]
    text = setups_text(rows)
    assert "🟢 **Set up 1 · IMÁN 15** (GHOST) · hoy 2 trades +$150" in text
    assert "⚪ **Set up 3 · IMÁN 1**" in text
