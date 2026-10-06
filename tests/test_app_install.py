"""GHOST installable as an app: the manifest, the service worker and the icons
are served at the root, without logging in (the phone fetches them first)."""
import json

from fastapi.testclient import TestClient

from signal_engine.api.accounts import Accounts
from signal_engine.api.app import STATIC_DIR, create_app
from signal_engine.api.backtest import BacktestManager
from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.config.schema import EngineConfig
from signal_engine.data.engine import DataEngine
from tests.test_bot_runtime import _runtime


def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    Accounts(tmp_path).add("Rickybobby", "una-clave-larga", "owner")     # the page is locked
    return TestClient(create_app(_runtime(DataEngine(EngineConfig.load())), settings=ServerSettings(),
                                 backtests=BacktestManager(tmp_path)))


def test_manifest_and_service_worker_are_public_and_valid(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/status").status_code == 401                        # locked indeed
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(m.text)
    assert data["display"] == "standalone" and data["start_url"].startswith("/")
    for icon in data["icons"]:
        assert (STATIC_DIR / icon["src"].split("/static/")[1]).exists()
    assert any(i.get("purpose") == "maskable" for i in data["icons"])
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert sw.headers.get("service-worker-allowed") == "/"
    assert '"/api/' in sw.text                                             # data is never cached
    assert c.get("/static/offline.html").status_code == 200


def test_pages_ask_to_be_installable():
    for page in ("index.html", "login.html"):
        html = (STATIC_DIR / page).read_text()
        assert 'rel="manifest"' in html and "apple-mobile-web-app-capable" in html
