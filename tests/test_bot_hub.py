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
    live = {s["name"]: s["live"] for s in d["strategies"] if s["status"] == "en vivo"}
    assert live["APERTURA"] == {"n": 2, "net_usd": 780.0}


def test_hub_flags_a_silent_orbit(tmp_path):
    _orbit_files(tmp_path, {"day": "2026-09-28"}, [], age_s=900)
    orbit = _client(tmp_path).get("/api/bots").json()["bots"][1]
    assert orbit["state"] == "stale" and orbit["now"][0]["kind"] == "alert"


def test_hub_without_orbit_installed(tmp_path):
    orbit = _client(tmp_path / "nothing").get("/api/bots").json()["bots"][1]
    assert orbit["state"] == "off" and "orbit on" in orbit["now"][0]["text"]
