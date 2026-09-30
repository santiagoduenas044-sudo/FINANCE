"""The practice-only link for a friend: /practica shows only the practice
screen, and its key opens only the bar replay — never the bots or controls."""
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.api.backtest import BacktestManager
from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.config.schema import EngineConfig
from signal_engine.data.engine import DataEngine
from tests.test_bot_runtime import _runtime


def _client(tmp_path, monkeypatch, token="owner"):
    if token:
        monkeypatch.setenv("DASHBOARD_TOKEN", token)
    else:
        monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    app = create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones"))),
                     settings=ServerSettings(), backtests=BacktestManager(tmp_path))
    return TestClient(app)


def test_practice_key_opens_only_the_replay(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    owner = {"Authorization": "Bearer owner"}
    assert c.post("/api/practice/link").status_code == 401            # only the owner makes links
    r = c.post("/api/practice/link", headers=owner).json()
    assert r["active"] and r["path"].startswith("/practica?token=")
    key = r["path"].split("token=")[1]
    friend = {"Authorization": f"Bearer {key}"}
    assert c.get("/practica").status_code == 200                      # the page shell itself
    assert c.get("/api/practice/datasets", headers=friend).status_code == 200
    assert c.get("/api/replay/bars?date=2026-01-05", headers=friend).status_code in (200, 400)
    for path in ("/api/status", "/api/bots", "/api/ghost/trades/history", "/api/backtest/options",
                 "/api/practice/link", "/api/system/version"):
        assert c.get(path, headers=friend).status_code == 401, path
    for path in ("/api/control/stop", "/api/system/update", "/api/practice/link"):
        assert c.post(path, headers=friend).status_code == 401, path
    # a new link replaces the old one; revoking closes it
    new_key = c.post("/api/practice/link", headers=owner).json()["path"].split("token=")[1]
    assert new_key != key
    assert c.get("/api/practice/datasets", headers=friend).status_code == 401
    c.post("/api/practice/revoke", headers=owner)
    assert c.get("/api/practice/datasets",
                 headers={"Authorization": f"Bearer {new_key}"}).status_code == 401


def test_no_link_while_the_dashboard_has_no_key(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, token=None)
    r = c.post("/api/practice/link")
    assert r.status_code == 409 and "DASHBOARD_TOKEN" in r.json()["detail"]
