"""The dashboard's bot hub (/api/bots): GHOST and ORBIT side by side, what each
one is doing, its record per version, and the strategy catalogue."""
import json
import os
import time

import pandas as pd
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.config.schema import EngineConfig
from signal_engine.data.engine import DataEngine
from signal_engine.versions import BOT_VERSIONS, current_version, version_by_date
from tests.test_bot_runtime import _runtime


def _client(orbit_dir):
    app = create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones"))))
    app.state.orbit_dir = orbit_dir
    return TestClient(app)


def _orbit_files(d, state, trades, age_s=5):
    d.mkdir(parents=True, exist_ok=True)
    (d / "apertura_state.json").write_text(json.dumps(state))
    (d / "apertura_trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades))
    t = time.time() - age_s
    os.utime(d / "apertura_state.json", (t, t))


def test_versions_registry():
    assert current_version("GHOST") == BOT_VERSIONS["GHOST"][-1][0]
    assert version_by_date("GHOST", "2026-09-26 10:00") == "v2"
    assert version_by_date("GHOST", pd.Timestamp("2026-09-29 10:00", tz="America/New_York")) == "v4"


def test_hub_shows_both_bots_and_orbit_state(tmp_path, monkeypatch):
    monkeypatch.delenv("ORBIT_POSITION", raising=False)
    today = pd.Timestamp.now(tz="America/New_York").date().isoformat()
    _orbit_files(tmp_path, {"day": today, "hi": 25100.0, "lo": 25050.0, "bars": 15,
                            "box_sent": True, "done": False, "pending": None, "trade": None,
                            "last_ts": f"{today} 10:02:00-04:00", "last_close": 25080.0},
                 [{"day": "2026-09-28", "usd": -120.0},                       # 1 MNQ era -> v1
                  {"day": "2026-09-29", "usd": 900.0, "symbol": "NQ", "pts": 45.0},     # 1 NQ -> v2
                  {"day": today, "usd": 450.0, "symbol": "MNQ", "contracts": 5, "why": "objetivo",
                   "pts": 45.0}])                                            # 5 MNQ -> v3
    d = _client(tmp_path).get("/api/bots").json()
    ghost, orbit = d["bots"]
    assert (ghost["name"], orbit["name"]) == ("GHOST", "ORBIT")
    assert ghost["versions"][0]["current"] and ghost["versions"][0]["version"] == current_version("GHOST")
    assert orbit["status"] == "ONLINE" and orbit["state"] == "watching"
    assert "Caja lista 25,050.00–25,100.00" in orbit["now"][0]["text"]
    assert orbit["total"]["n"] == 3 and orbit["total"]["net_usd"] == 1230.0
    per = {v["version"]: v for v in orbit["versions"]}
    assert per["v1"]["net_usd"] == -120.0 and per["v2"]["net_usd"] == 900.0
    assert per["v3"]["net_usd"] == 450.0
    assert orbit["today"]["n"] == 1
    # the "principal" row counts only the version running now (v3), not the older ones
    assert orbit["main"]["version"] == "v3" and orbit["main"]["total"]["net_usd"] == 450.0
    assert ghost["main"]["version"] == current_version("GHOST")
    live = {s["code"]: s["live"] for s in d["strategies"] if s["status"] in ("en vivo", "descartada")}
    # each version on its own, never added up into one "strategy total"
    assert "n" not in live["APERTURA"] and "net_usd" not in live["APERTURA"]
    assert [(p["version"], p["net_usd"]) for p in live["APERTURA"]["detail"]] == \
        [("v1", -120.0), ("v2", 900.0), ("v3", 450.0)]
    assert "combined_usd" not in d                                  # no "together" total


def test_hub_flags_a_silent_orbit(tmp_path):
    _orbit_files(tmp_path, {"day": "2026-09-28"}, [], age_s=900)
    orbit = _client(tmp_path).get("/api/bots").json()["bots"][1]
    assert orbit["state"] == "stale" and orbit["now"][0]["kind"] == "alert"


def test_hub_without_orbit_installed(tmp_path):
    orbit = _client(tmp_path / "nothing").get("/api/bots").json()["bots"][1]
    assert orbit["state"] == "off" and "orbit on" in orbit["now"][0]["text"]


# --------------------------------------------------------------------------- #
# parallel variants (config/shadows.yaml)
# --------------------------------------------------------------------------- #
def test_shadow_specs_and_config_overrides():
    from signal_engine.runtime.shadows import load_specs
    ghost = {s.id: s for s in load_specs("ghost")}
    assert ghost["zonas_1m"].strategy == "zones_1m"
    cfg = EngineConfig.load(strategy="zones_1m", overrides=ghost["zonas_1m_noche"].overrides)
    assert cfg.setups.zone_retrace.timeframe == "1m" and cfg.timeframe_roles.structure == "5m"
    assert cfg.ghost.valid_sessions == ["RTH", "ETH"] and cfg.ghost.daily_loss_limit_usd is None
    assert {s.id for s in load_specs("orbit")} == {"apertura_30m", "apertura_tp2"}


def test_ghost_shadows_get_the_live_bars(tmp_path):
    from signal_engine.data.contracts import ContractRegistry
    from signal_engine.runtime.shadows import build_ghost_shadows
    from tests.test_bot_runtime import _pipeline

    shadows = build_ghost_shadows(ContractRegistry.load(), base=tmp_path)
    assert [s.id for s, _ in shadows] == ["zonas_1m_noche", "zonas_1m_europa"]   # retired ones never run
    engine = DataEngine(EngineConfig.load(strategy="zones"))
    rt = _runtime(engine)
    rt.shadows = shadows
    rt.start()
    lc = _pipeline(engine)
    for ts in lc.timeframes["1m"].index[400:460]:
        rt.run_cycle("ES", {tf: df.loc[:ts] for tf, df in lc.timeframes.items()})
    for _spec, srt in shadows:
        assert srt.last_run_at is not None                     # each variant ran its own cycle
        assert srt.output_dispatcher.adapters == []           # and never talks to Discord
    assert (tmp_path / "ghost" / "zonas_1m_noche").is_dir()


def test_orbit_feeds_its_variants(tmp_path):
    from signal_engine.subbots import orbit

    seen = []

    class Fake:
        def __init__(self): self.bars = None
        def on_bars(self, bars): seen.append(bars)

    spec = type("S", (), {"id": "x"})()
    assert orbit.run_once(Fake(), fetch=lambda: "bars", shadows=[(spec, Fake())])
    assert seen == ["bars", "bars"]
    assert orbit.build_shadows(tmp_path) == []          # APERTURA 30 and 2× are retired


def test_hub_lists_the_variants(tmp_path):
    app = create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones"))))
    app.state.orbit_dir, app.state.shadows_dir = tmp_path / "o", tmp_path / "sh"
    d = TestClient(app).get("/api/bots").json()
    orbit = d["bots"][1]
    assert [v["id"] for v in orbit["variants"]] == ["apertura_30m", "apertura_tp2"]
    assert all(v["retired"] and not v["enabled"] for v in orbit["variants"])
    assert orbit["retired"] is True
    assert any(s["status"] == "en paralelo" for s in d["strategies"])


# --------------------------------------------------------------------------- #
# the on/off switches
# --------------------------------------------------------------------------- #
def test_switches_default_and_persist(tmp_path):
    from signal_engine.runtime.shadows import is_on, load_specs, set_on
    spec = {s.id: s for s in load_specs("ghost")}
    assert is_on("ghost", spec["zonas_1m_noche"], tmp_path)           # on by default
    set_on("ghost", "zonas_1m_noche", False, tmp_path)
    assert not is_on("ghost", spec["zonas_1m_noche"], tmp_path)
    set_on("ghost", "zonas_1m_noche", True, tmp_path)
    assert is_on("ghost", spec["zonas_1m_noche"], tmp_path)
    set_on("ghost", "setups_clasicos", True, tmp_path)                # retired: stays off
    assert not is_on("ghost", spec["setups_clasicos"], tmp_path)


def test_dashboard_switch_starts_and_stops_ghost_variants(tmp_path):
    rt = _runtime(DataEngine(EngineConfig.load(strategy="zones")))
    app = create_app(rt)
    app.state.orbit_dir, app.state.shadows_dir = tmp_path / "o", tmp_path
    c = TestClient(app)
    assert c.post("/api/bots/ghost/variants/zonas_1m_noche?on=true").json()["on"] is True
    c.post("/api/bots/ghost/variants/zonas_1m_europa?on=true")
    assert {s.id for s, _ in rt.shadows} == {"zonas_1m_noche", "zonas_1m_europa"}   # several at once
    c.post("/api/bots/ghost/variants/zonas_1m_noche?on=false")
    assert [s.id for s, _ in rt.shadows] == ["zonas_1m_europa"]
    r = c.post("/api/bots/ghost/variants/setups_clasicos?on=true")              # retired
    assert r.status_code == 409 and "retirado" in r.json()["detail"]
    ghost = c.get("/api/bots").json()["bots"][0]
    v = {x["id"]: x for x in ghost["variants"]}
    assert v["zonas_1m_europa"]["enabled"] and v["zonas_1m_europa"]["running"]
    assert not v["zonas_1m_noche"]["enabled"] and v["zonas_1m_noche"]["now"] == "Apagado."
    assert v["setups_clasicos"]["retired"]
    vers = {x["version"]: x for x in ghost["versions"]}
    assert vers["v1"]["variant"] == "setups_clasicos" and vers["v1"]["variant_on"] is False
    assert vers["v3"]["variant_on"] is False and vers["v7"]["current"]
    assert vers["v4"]["variant"] == "zonas_15m_fijo"                 # the fixed-stop Set up 9
    # ORBIT runs elsewhere: the switch is saved for it to pick up
    assert c.post("/api/bots/orbit/variants/apertura_tp2?on=true").status_code == 409
    assert c.post("/api/bots/ghost/variants/nope?on=true").status_code == 404


def test_five_minute_variant_never_sees_a_forming_candle(tmp_path):
    from signal_engine.data.contracts import ContractRegistry
    from signal_engine.runtime.shadows import (build_ghost_shadow, load_specs,
                                               run_ghost_shadows)
    from tests.test_bot_runtime import _pipeline

    spec = next(s for s in load_specs("ghost") if s.id == "setups_clasicos")
    srt = build_ghost_shadow(spec, ContractRegistry.load(), base=tmp_path)
    assert srt.data_engine.cfg.timeframe_roles.execution == "5m"
    engine = DataEngine(EngineConfig.load(strategy="zones"))
    tf = _pipeline(engine).timeframes
    seen = []
    orig = srt.run_cycle
    srt.run_cycle = lambda sym, bars: (seen.append(bars["5m"].index[-1]), orig(sym, bars))
    for ts in tf["1m"].index[300:312]:
        run_ghost_shadows([(spec, srt)], "ES", {k: df for k, df in tf.items()}, None, ts)
        assert all(t <= ts for t in seen)                     # nothing that closes later
    assert len(seen) == len(set(seen)) <= 3                   # once per 5-minute candle


def test_orbit_follows_the_switches(tmp_path, monkeypatch):
    from dataclasses import replace
    from signal_engine.runtime import shadows as sh
    from signal_engine.runtime.shadows import set_on
    from signal_engine.subbots import orbit
    real = sh.load_specs
    # the switches themselves, with APERTURA's variants as if not retired
    monkeypatch.setattr(sh, "load_specs", lambda bot, *a, **k: [replace(x, retired=False)
                                                                  for x in real(bot, *a, **k)])
    running = orbit.build_shadows(tmp_path)
    assert {s.id for s, _ in running} == {"apertura_30m", "apertura_tp2"}
    set_on("orbit", "apertura_30m", False, tmp_path)
    after = orbit.build_shadows(tmp_path, current=running)
    assert [s.id for s, _ in after] == ["apertura_tp2"]
    tp2 = next(b for s, b in running if s.id == "apertura_tp2")
    assert after[0][1] is tp2                                  # the running one is kept as is


def test_old_trades_go_to_the_version_that_really_made_them():
    from types import SimpleNamespace as T
    from signal_engine.versions import ghost_version_of

    def t(setup, tf, when, **ctx):
        return T(setup=setup, timeframe=tf, opened_at=pd.Timestamp(when, tz="America/New_York"),
                 context=ctx)
    assert ghost_version_of(t("vwap_reclaim", "5m", "2026-09-28 10:00")) == "v1"
    assert ghost_version_of(t("zone_retrace", "15m", "2026-09-29 14:45")) == "v4"
    assert ghost_version_of(t("zone_retrace", "1m", "2026-09-25 10:05")) == "v2"
    assert ghost_version_of(t("zone_retrace", "1m", "2026-09-28 10:22")) == "v3"   # 1 min, 27-28 sep
    assert ghost_version_of(t("zone_retrace", "1m", "2026-09-30 21:00")) == "v3"   # 1 min at night
    assert ghost_version_of(t("zone_retrace", "1m", "2026-09-28 10:22", bot_version="v9")) == "v9"


def test_hub_survives_a_missing_price_and_a_broken_bot(tmp_path, monkeypatch):
    today = pd.Timestamp.now(tz="America/New_York").date().isoformat()
    # ORBIT saw a bar with no price (Yahoo gaps): NaN used to make /api/bots fail
    trade = {"id": "APERTURA-x", "day": today, "long": True, "entry": 25000.0, "stop": 24950.0,
             "stop_initial": 24950.0, "target": 25075.0, "risk": 50.0, "be_done": False,
             "opened_at": f"{today} 09:50:00-04:00", "box_hi": 25010.0, "box_lo": 24950.0,
             "signal_ts": f"{today} 09:50:00-04:00"}
    _orbit_files(tmp_path, {"day": today, "hi": 25010.0, "lo": 24950.0, "bars": 15,
                            "box_sent": True, "done": True, "pending": None, "trade": trade,
                            "last_ts": f"{today} 10:02:00-04:00", "last_close": float("nan")}, [])
    c = _client(tmp_path)
    r = c.get("/api/bots")
    assert r.status_code == 200
    assert r.json()["bots"][1]["open_usd"] is None
    # a bot whose card cannot be built shows its error; the other one still shows
    from signal_engine.api import bots
    monkeypatch.setattr(bots, "ghost_card", lambda *a, **k: 1 / 0)
    d = c.get("/api/bots").json()
    assert d["bots"][0]["error"].startswith("ZeroDivisionError")
    assert d["bots"][1]["name"] == "ORBIT" and "error" not in d["bots"][1]


# --------------------------------------------------------------------------- #
# each strategy counts only its own trades; the calendar's trade log
# --------------------------------------------------------------------------- #
def _ghost_trade(when, tf, pts, n):
    from signal_engine.paper.models import ExitReason, PaperTrade
    opened = pd.Timestamp(when, tz="America/New_York")
    t = PaperTrade(signal_id=f"s{n}", instrument="NQ", direction="LONG", setup="zone_retrace",
                   entry=20000.0, stop=19970.0, take_profits=[20090.0], opened_at=opened,
                   initial_risk=30.0, timeframe=tf)
    t.pnl_points, t.closed_at = pts, opened + pd.Timedelta(minutes=20)
    t.exit_price, t.exit_reason = 20000.0 + pts, ExitReason.STOP
    return t


def test_each_strategy_counts_only_its_own_trades(tmp_path):
    from signal_engine.paper.tracker import PaperTradeTracker
    rt = _runtime(DataEngine(EngineConfig.load(strategy="zones")))
    rt.paper_tracker = PaperTradeTracker(store_dir=tmp_path / "paper")
    rt.paper_tracker.closed_trades += [
        _ghost_trade("2026-09-25 10:05", "1m", 40.0, 1),      # v2 · FVG 1 DÍA
        _ghost_trade("2026-09-25 11:05", "1m", 60.0, 2),      # v2
        _ghost_trade("2026-09-28 21:05", "1m", -10.0, 3),     # v3 · FVG 1
        _ghost_trade("2026-09-30 10:45", "15m", 25.0, 4)]     # v4 · FVG 15
    app = create_app(rt)
    app.state.orbit_dir, app.state.shadows_dir = tmp_path / "o", tmp_path / "sh"
    c = TestClient(app)
    st = {s["code"]: s for s in c.get("/api/bots").json()["strategies"]}
    # each part on its own: v4 (stop fijo), v7 (la de ahora), Set up 9; v5, v6 had no trades here
    parts = {p.get("version") or p.get("variant"): p for p in st["FVG 15"]["live"]["detail"]}
    only = parts["v4"]
    pv = only["net_usd"] / 25.0                                   # $ per point of GHOST's size
    assert only["n"] == 1                                         # not the 1-minute trades
    assert parts["v7"]["n"] == 0 and parts["v7"]["name"].startswith("Set up 1") and "v5" not in parts and "v6" not in parts
    assert parts["zonas_15m_fijo"]["name"].startswith("Set up 9")
    real = [p for p in st["FVG 1 DÍA"]["live"]["detail"] if p["kind"] == "real"]
    assert real == [{"kind": "real", "bot": "GHOST", "version": "v2",
                     "name": "GHOST v2 (antes)", "n": 2, "net_usd": 100.0 * pv}]
    assert st["FVG 1 DÍA"]["live"]["detail"][-1]["kind"] == "paper"  # plus its parallel run
    assert st["FVG 1 DÍA"]["live"]["detail"][-1]["name"].startswith("Set up 3")
    assert st["FVG 15"]["setups"] == [1, 9] and st["FVG 1 DÍA"]["setups"] == [3]
    assert [p["n"] for p in st["FVG 1"]["live"]["detail"] if p["kind"] == "real"] == [1]
    assert st["CLÁSICO"]["status"] == "apagada"                  # its variant is off
    assert st["RELLENO"]["status"] == "por probar" and st["RELLENO"]["lab_run"] is None
    # the trade log behind the calendar: every trade, with its version and day
    log = c.get("/api/bots/trades").json()
    mine = [t for t in log["trades"] if t["src"] == "ghost"]
    assert [t["version"] for t in mine] == ["v2", "v2", "v3", "v4"]
    assert mine[0]["day"] == "2026-09-25" and mine[0]["usd"] == 40.0 * pv
    ids = {s["id"] for s in log["sources"]}
    assert {"ghost", "orbit", "ghost:zonas_1m", "orbit:apertura_30m"} <= ids
    assert all("fondeo" in s for s in log["sources"])


def test_old_orbit_trades_keep_the_size_they_opened_with(tmp_path):
    from signal_engine.subbots.opening_range import OpeningRangeBot, OrbSettings
    rows = [  # closed after an update, so saved with the NEW size: the real one is put back
        {"id": "a", "day": "2026-09-29", "opened_at": "2026-09-29 09:50:00-04:00", "long": True,
         "pts": 30.0, "usd": 9999.0, "contracts": 1, "symbol": "NQ"},          # was 1 MNQ
        {"id": "b", "day": "2026-09-30", "opened_at": "2026-09-30 09:55:00-04:00", "long": True,
         "pts": 30.0, "usd": 9999.0, "contracts": 5, "symbol": "MNQ"},         # was 1 NQ
        {"id": "c", "day": "2026-10-01", "opened_at": "2026-10-01 09:55:00-04:00", "long": True,
         "pts": 30.0, "usd": 287.0, "contracts": 5, "symbol": "MNQ", "usd_per_point": 2.0,
         "cost_points": 1.02}]                                                 # stored: as is
    _orbit_files(tmp_path, {"day": "2026-10-01"}, rows)
    closed = OpeningRangeBot(tmp_path, settings=OrbSettings.for_position("5 MNQ")).closed
    mnq, nq = OrbSettings.for_position("1 MNQ"), OrbSettings.for_position("1 NQ")
    assert (closed[0]["symbol"], closed[0]["contracts"]) == ("MNQ", 1)
    assert closed[0]["usd"] == round((30.0 - mnq.cost_points) * 2.0, 2)
    assert (closed[1]["symbol"], closed[1]["contracts"]) == ("NQ", 1)
    assert closed[1]["usd"] == round((30.0 - nq.cost_points) * 20.0, 2)
    assert closed[2]["usd"] == 287.0
    from signal_engine.versions import orbit_version_of
    assert [orbit_version_of(c) for c in closed] == ["v1", "v2", "v3"]


def test_catalogue_shows_the_last_lab_run_of_each_new_idea(tmp_path):
    from signal_engine.api.bots import strategy_cards
    row = lambda name, ok, avg: {"name": name, "ok": ok, "train": {"n": 80, "avg": avg},  # noqa: E731
                                 "test": {"n": 80, "avg": avg, "conf": 1.5 if ok else 0.2},
                                 "all": {"n": 160, "net": avg * 160, "dd": 900.0}}
    (tmp_path / "20261001-200000.json").write_text(json.dumps({"meta": {"label": "5 MNQ"}, "rows": [
        row("MURO · rango 2 h · stop otro lado · TP 2×", False, -3.0),
        row("MURO · rango 4 h · stop tras EMA 20 · TP 2×", True, 14.0),
        row("RELLENO · hueco ≥ 20 pts", False, -1.0)]}))
    (tmp_path / "20261002-090000.json").write_text(json.dumps({"meta": {}, "rows": [
        row("Zonas 15 min · TP 3× stop", True, 9.0)]}))           # a newer run of another study
    cards = {c["code"]: c for c in strategy_cards([], tmp_path)}
    muro = cards["MURO"]["lab_run"]
    assert (muro["variants"], muro["passed"]) == (2, 1)
    assert muro["best"]["name"].endswith("stop tras EMA 20 · TP 2×") and muro["best"]["net_usd"] == 2240.0
    assert cards["RELLENO"]["lab_run"]["passed"] == 0
    assert cards["TRAMPA"]["lab_run"] is None                       # not tried yet


def test_funded_check_reads_one_set_up_against_the_limit():
    from signal_engine.api.bots import funded_check
    rows = [{"day": "2026-10-01", "net_usd": 500.0, "net_pts": 50.0},
            {"day": "2026-10-02", "net_usd": -400.0, "net_pts": -40.0},
            {"day": "2026-10-02", "net_usd": -300.0, "net_pts": -30.0},
            {"day": "2026-10-03", "net_usd": 200.0, "net_pts": 20.0}]
    fo = funded_check(rows, limit=1500)
    assert fo["n"] == 4 and fo["few"] and fo["fits"]
    assert fo["worst_trade_usd"] == -400 and fo["worst_day_usd"] == -700
    assert fo["max_dd_usd"] == 700                     # from +500 down to −200
    assert fo["max_dd_1mnq_usd"] == 140 and fo["mnq"] == 10   # 70 pts x $2


def test_funded_check_says_when_one_trade_breaks_the_account():
    from signal_engine.api.bots import funded_check
    rows = [{"day": f"2026-10-0{i}", "net_usd": -1984.0, "net_pts": -198.4} for i in (1, 2, 3)]
    fo = funded_check(rows, limit=1500)
    assert not fo["fits"] and fo["max_dd_usd"] == 5952 and fo["mnq"] == 1
    assert funded_check([], limit=1500)["fits"] is None


def test_record_pdf_lists_each_set_up_on_its_own(tmp_path):
    from signal_engine.output.record_pdf import build_pdf, setups_of
    log = {"sources": [
        {"id": "ghost", "setup": 1, "setup_label": "Set up 1 · FVG 15", "kind": "main",
         "current": "v5", "bot": "GHOST", "position": "5 MNQ",
         "fondeo": {"account": "25K", "n": 1, "fits": True, "few": True}},
        {"id": "ghost:zonas_1m_noche", "setup": 4, "name": "Set up 4 · FVG 1",
         "kind": "variant", "bot": "GHOST", "enabled": True}],
        "trades": [
        {"src": "ghost", "version": "v4", "day": "2026-10-01", "net_usd": -999.0},   # older version
        {"src": "ghost", "version": "v5", "day": "2026-10-06", "opened_at": "2026-10-06T10:00:00-04:00",
         "closed_at": "2026-10-06T10:40:00-04:00", "side": "LONG", "entry": 1.0, "exit": 2.0,
         "pts": 1.0, "net_usd": 290.0, "why": "TP2"},
        {"src": "ghost:zonas_1m_noche", "day": "2026-10-06", "opened_at": "2026-10-06T21:30:00-04:00",
         "closed_at": "2026-10-06T22:00:00-04:00", "side": "SHORT", "entry": 2.0, "exit": 1.0,
         "pts": 1.0, "net_usd": 805.0, "why": "TP2"}]}
    sus = setups_of(log)
    assert [(so["setup"], [t["net_usd"] for t in T]) for so, T in sus] == [(1, [290.0]), (4, [805.0])]
    pdf = build_pdf(log)
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000
    assert build_pdf(log, only=4).startswith(b"%PDF")


def test_record_pdf_endpoint(tmp_path):
    app = create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones"))))
    app.state.orbit_dir, app.state.shadows_dir = tmp_path / "o", tmp_path / "sh"
    r = TestClient(app).get("/api/record.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]


def test_pdf_says_where_one_set_up_wins_and_loses():
    from signal_engine.output.record_pdf import where_lines
    T = [{"opened_at": "2026-10-06T21:30:00-04:00", "net_usd": 805.0, "side": "LONG"},
         {"opened_at": "2026-10-06T10:30:00-04:00", "net_usd": -330.0, "side": "SHORT"},
         {"opened_at": "2026-10-07T01:00:00-04:00", "net_usd": 700.0, "side": "LONG"}]
    *hours, (lado, sides) = where_lines(T)
    hours = " ".join(text for _label, text in hours)
    assert lado == "Por lado"
    assert "Noche 2 trades +$1,505" in hours and "Mañana NY 1 trade −$330" in hours
    assert "Europa" not in hours                                  # only where it traded
    assert sides.endswith("Compras 2 trades +$1,505   ·   Ventas 1 trade −$330")
    assert where_lines(T[:1]) == []
