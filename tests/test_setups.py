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
    assert text.splitlines() == ["✅ **Entrada 20,000.00**",
                                 "🛑 Stop 19,975.00 · 25.0 pts · −$250",
                                 "🎯 Objetivo 20,075.00 · 75.0 pts · +$750"]
    tr.update_symbol("NQ", _bar(20_000, 20_080, 19_990, 20_070), T0 + pd.Timedelta(minutes=1))
    title, text, _ = alert_note("Set up 3 · IMÁN 1", "close", t, pv, tr.closed_trades)
    assert title == "✅ Set up 3 · IMÁN 1 — objetivo · +75.00 pts"
    first, record = text.splitlines()
    assert first == "20,000.00 → 20,075.00 · **+$750**"
    assert record.startswith("Récord: 1 trade · ganó 1 · **+$7") and "con comisiones" in record


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


def test_ghost_set_up_alerts_carry_a_chart(tmp_path):
    """Set ups 3, 4, 5 and 9 send their entry and exit with a chart, like Set up 1."""
    from types import MethodType, SimpleNamespace
    from signal_engine.config.schema import EngineConfig
    from signal_engine.runtime.engine import BotRuntime

    idx = pd.date_range(T0 - pd.Timedelta(minutes=59), periods=60, freq="1min")
    one = pd.DataFrame({"open": 20_000.0, "high": 20_010.0, "low": 19_990.0,
                        "close": 20_000.0}, index=idx)
    spec = ShadowSpec(id="zonas_1m_noche", name="IMÁN 1 NOCHE", setup=4)
    shadow = _FakeRuntime(tmp_path)
    shadow._point_values = lambda s: {"5 MNQ": 10.0}
    sent = []
    main = SimpleNamespace(data_engine=SimpleNamespace(cfg=EngineConfig.load(strategy="zones")),
                           output_dispatcher=SimpleNamespace(
                               publish_note=lambda **kw: sent.append(kw)),
                           _point_values=lambda s: {"5 MNQ": 10.0})
    for name in ("_shadow_alert", "_shadow_chart", "_trade_chart", "_close_chart"):
        setattr(main, name, MethodType(getattr(BotRuntime, name), main))
    main._close_title = BotRuntime._close_title
    run_ghost_shadows([(spec, shadow)], "NQ", {"1m": one}, T0, notify=main._shadow_alert)
    shadow.paper_tracker.update_symbol("NQ", _bar(1.0, 1.0, -1.0, -1.0),
                                       T0 + pd.Timedelta(minutes=1))
    trade = shadow.paper_tracker.closed_trades[-1]
    main._shadow_alert(spec, shadow, "close", trade)
    assert [s["key"].rsplit(":", 1)[-1] for s in sent] == ["open", "close"]
    assert all(s["image"] and s["image"].startswith(b"\x89PNG") for s in sent)


def test_a_set_up_alert_without_bars_still_goes_out():
    from types import MethodType, SimpleNamespace
    from signal_engine.config.schema import EngineConfig
    from signal_engine.runtime.engine import BotRuntime

    sent = []
    main = SimpleNamespace(data_engine=SimpleNamespace(cfg=EngineConfig.load(strategy="zones")),
                           output_dispatcher=SimpleNamespace(
                               publish_note=lambda **kw: sent.append(kw)),
                           _point_values=lambda s: None)
    for name in ("_shadow_alert", "_shadow_chart", "_trade_chart"):
        setattr(main, name, MethodType(getattr(BotRuntime, name), main))
    shadow = SimpleNamespace(_point_values=lambda s: None, paper_tracker=None)
    trade = PaperTradeTracker().open_trade(signal_id="x", instrument="NQ", direction="SHORT",
                                           setup="zone_retrace", entry=1.0, stop=2.0,
                                           take_profits=[0.0], opened_at=T0)
    main._shadow_alert(ShadowSpec(id="z", name="IMÁN 1", setup=3), shadow, "open", trade)
    assert len(sent) == 1 and sent[0]["image"] is None


def test_paper_fills_cost_a_tick_and_honour_gaps(tmp_path):
    """Same rule as ORBIT: a market entry and a stop fill 1 tick worse; a bar
    that opens past the stop fills at its open; a target fills at its price."""
    tr = PaperTradeTracker(store_dir=tmp_path, slippage_points=0.25, gap_fills=True)
    t = tr.open_trade(signal_id="g", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=100.0, stop=80.0, take_profits=[160.0], opened_at=T0)
    assert t.entry == 100.25 and t.context["signal_entry"] == 100.0
    tr.update_symbol("NQ", _bar(70, 72, 65, 68), T0 + pd.Timedelta(minutes=1))   # gaps under 80
    assert t.exit_price == 69.75 and t.pnl_points == -30.5
    w = tr.open_trade(signal_id="h", instrument="NQ", direction="SHORT", setup="zone_retrace",
                      entry=100.0, stop=120.0, take_profits=[40.0], opened_at=T0)
    tr.update_symbol("NQ", _bar(60, 61, 39, 41), T0 + pd.Timedelta(minutes=2))
    assert w.entry == 99.75 and w.exit_price == 40.0 and w.pnl_points == 59.75


def test_live_strategies_fill_like_a_real_account():
    from signal_engine.config.schema import EngineConfig
    for name in ("zones", "zones_1m", "zones_fijo"):
        g = EngineConfig.load(strategy=name).ghost
        assert g.slippage_points == 0.25 and g.gap_fills
