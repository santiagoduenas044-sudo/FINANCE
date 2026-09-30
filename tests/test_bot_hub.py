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
                  {"day": today, "usd": 900.0, "symbol": "NQ", "why": "objetivo", "pts": 45.0}])
    d = _client(tmp_path).get("/api/bots").json()
    ghost, orbit = d["bots"]
    assert (ghost["name"], orbit["name"]) == ("GHOST", "ORBIT")
    assert ghost["versions"][0]["current"] and ghost["versions"][0]["version"] == current_version("GHOST")
    assert orbit["status"] == "ONLINE" and orbit["state"] == "watching"
    assert "Caja lista 25,050.00–25,100.00" in orbit["now"][0]["text"]
    assert orbit["total"]["n"] == 2 and orbit["total"]["net_usd"] == 780.0
    per = {v["version"]: v for v in orbit["versions"]}
    assert per["v1"]["net_usd"] == -120.0 and per["v2"]["net_usd"] == 900.0
    assert orbit["today"]["n"] == 1
    # the "principal" row counts only the version running now (v2), not v1's trades
    assert orbit["main"]["version"] == "v2" and orbit["main"]["total"]["net_usd"] == 900.0
    assert ghost["main"]["version"] == current_version("GHOST")
    live = {s["name"]: s["live"] for s in d["strategies"] if s["status"] == "en vivo"}
    assert live["APERTURA"] == {"n": 2, "net_usd": 780.0}


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
    assert [s.id for s, _ in shadows] == ["zonas_1m", "zonas_1m_noche"]
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
    assert (tmp_path / "ghost" / "zonas_1m").is_dir()


def test_orbit_feeds_its_variants(tmp_path):
    from signal_engine.subbots import orbit

    seen = []

    class Fake:
        def __init__(self): self.bars = None
        def on_bars(self, bars): seen.append(bars)

    spec = type("S", (), {"id": "x"})()
    assert orbit.run_once(Fake(), fetch=lambda: "bars", shadows=[(spec, Fake())])
    assert seen == ["bars", "bars"]
    variants = orbit.build_shadows(tmp_path)
    assert {s.id: b.s.range_minutes for s, b in variants} == {"apertura_30m": 30, "apertura_tp2": 15}


def test_hub_lists_the_variants(tmp_path):
    app = create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones"))))
    app.state.orbit_dir, app.state.shadows_dir = tmp_path / "o", tmp_path / "sh"
    d = TestClient(app).get("/api/bots").json()
    orbit = d["bots"][1]
    assert [v["id"] for v in orbit["variants"]] == ["apertura_30m", "apertura_tp2"]
    assert orbit["variants"][0]["now"].startswith("Todavía no ha corrido")
    assert any(s["status"] == "en paralelo" for s in d["strategies"])


# --------------------------------------------------------------------------- #
# the on/off switches
# --------------------------------------------------------------------------- #
def test_switches_default_and_persist(tmp_path):
    from signal_engine.runtime.shadows import is_on, load_specs, set_on
    spec = {s.id: s for s in load_specs("ghost")}
    assert not is_on("ghost", spec["setups_clasicos"], tmp_path)      # off until switched on
    assert is_on("ghost", spec["zonas_1m"], tmp_path)
    set_on("ghost", "setups_clasicos", True, tmp_path)
    set_on("ghost", "zonas_1m", False, tmp_path)
    assert is_on("ghost", spec["setups_clasicos"], tmp_path)
    assert not is_on("ghost", spec["zonas_1m"], tmp_path)


def test_dashboard_switch_starts_and_stops_ghost_variants(tmp_path):
    rt = _runtime(DataEngine(EngineConfig.load(strategy="zones")))
    app = create_app(rt)
    app.state.orbit_dir, app.state.shadows_dir = tmp_path / "o", tmp_path
    c = TestClient(app)
    assert c.post("/api/bots/ghost/variants/setups_clasicos?on=true").json()["on"] is True
    assert [s.id for s, _ in rt.shadows] == ["setups_clasicos"]
    c.post("/api/bots/ghost/variants/zonas_1m?on=true")
    assert {s.id for s, _ in rt.shadows} == {"setups_clasicos", "zonas_1m"}   # several at once
    c.post("/api/bots/ghost/variants/setups_clasicos?on=false")
    assert [s.id for s, _ in rt.shadows] == ["zonas_1m"]
    ghost = c.get("/api/bots").json()["bots"][0]
    v = {x["id"]: x for x in ghost["variants"]}
    assert v["zonas_1m"]["enabled"] and v["zonas_1m"]["running"]
    assert not v["setups_clasicos"]["enabled"] and v["setups_clasicos"]["now"] == "Apagada."
    vers = {x["version"]: x for x in ghost["versions"]}
    assert vers["v1"]["variant"] == "setups_clasicos" and vers["v1"]["variant_on"] is False
    assert vers["v2"]["variant_on"] is True and vers["v4"]["current"]
    # ORBIT runs elsewhere: the switch is saved for it to pick up
    assert c.post("/api/bots/orbit/variants/apertura_tp2?on=false").status_code == 200
    from signal_engine.runtime.shadows import switches
    assert switches(tmp_path)["orbit"]["apertura_tp2"] is False
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


def test_orbit_follows_the_switches(tmp_path):
    from signal_engine.runtime.shadows import set_on
    from signal_engine.subbots import orbit
    running = orbit.build_shadows(tmp_path)
    assert {s.id for s, _ in running} == {"apertura_30m", "apertura_tp2"}
    set_on("orbit", "apertura_30m", False, tmp_path)
    after = orbit.build_shadows(tmp_path, current=running)
    assert [s.id for s, _ in after] == ["apertura_tp2"]
    tp2 = next(b for s, b in running if s.id == "apertura_tp2")
    assert after[0][1] is tp2                                  # the running one is kept as is
