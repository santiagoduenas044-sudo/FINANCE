"""Tests for Phase 13 — remote/server configuration, auth placeholder, CORS,
and the dashboard health check.

No secrets are invented: the auth token is read from the environment (patched
here), never hard-coded, and never returned in any response.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.runtime.engine import BotRuntime
from signal_engine.runtime.service import BotService
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

SAMPLES = Path(__file__).resolve().parent / "data_samples"


def _runtime(engine, registry):
    raw = CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26")
    hist = normalize(raw, engine.cfg.data, "1m")
    feed = LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=100000),
                    "ES", engine.cfg)
    feed.poll()
    return BotRuntime(engine, SetupEngine(engine.cfg, registry),
                      SignalEngine(engine.cfg.signals), registry,
                      live_feeds={"ES": feed})


# --------------------------------------------------------------------------- #
# ServerSettings
# --------------------------------------------------------------------------- #
def test_settings_defaults_are_local_and_safe():
    s = ServerSettings()
    assert s.mode == "local" and s.host == "127.0.0.1" and s.port == 8000
    assert s.auth_enabled is False
    s.validate()  # local with no auth is fine


def test_settings_from_env_parses_everything(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "remote")
    monkeypatch.setenv("BOT_PORT", "9001")
    monkeypatch.setenv("BOT_SYMBOLS", "ES, NQ ,YM")
    monkeypatch.setenv("BOT_CYCLE_SECONDS", "3.5")
    monkeypatch.setenv("BOT_AUTO_START", "true")
    monkeypatch.setenv("BOT_CORS_ORIGINS", "https://a.com, https://b.com")
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    s = ServerSettings.from_env()
    assert s.mode == "remote"
    assert s.host == "0.0.0.0"          # remote default bind
    assert s.port == 9001
    assert s.symbols == ["ES", "NQ", "YM"]
    assert s.cycle_seconds == 3.5
    assert s.auto_start is True
    assert s.cors_origins == ["https://a.com", "https://b.com"]
    assert s.auth_enabled is True


def test_remote_mode_requires_a_token(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        ServerSettings(mode="remote").validate()
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    ServerSettings(mode="remote").validate()  # now ok


def test_cors_wildcard_is_rejected():
    with pytest.raises(RuntimeError):
        ServerSettings(cors_origins=["*"]).validate()


def test_public_dict_never_leaks_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    s = ServerSettings(mode="remote")
    pub = s.public_dict()
    assert pub["auth_enabled"] is True
    assert "supersecret" not in str(pub)
    assert "token" not in pub


# --------------------------------------------------------------------------- #
# Health check (always unauthenticated, secret-free)
# --------------------------------------------------------------------------- #
def test_health_endpoint_ok_and_secret_free(engine, registry):
    rt = _runtime(engine, registry)
    c = TestClient(create_app(rt))
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "OFFLINE"
    assert "connection" in body and "uptime_seconds" in body
    assert body["symbols"] == ["ES"]


def test_health_stays_open_even_when_auth_enabled(engine, registry, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "abc")
    settings = ServerSettings(mode="remote")
    c = TestClient(create_app(_runtime(engine, registry), settings=settings))
    assert c.get("/api/health").status_code == 200         # exempt
    assert c.get("/api/status").status_code == 401         # protected


# --------------------------------------------------------------------------- #
# Auth gate (placeholder bearer token)
# --------------------------------------------------------------------------- #
def test_auth_disabled_by_default(engine, registry):
    c = TestClient(create_app(_runtime(engine, registry)))
    assert c.get("/api/status").status_code == 200  # local mode, no token needed


def test_auth_gate_enforced_when_token_set(engine, registry, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "letmein")
    settings = ServerSettings(mode="remote")
    c = TestClient(create_app(_runtime(engine, registry), settings=settings))
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer letmein"}).status_code == 200
    assert c.get("/api/status?token=letmein").status_code == 200   # phone-friendly
    assert c.get("/").status_code == 200                            # UI shell exempt


def test_token_never_appears_in_responses(engine, registry, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "zzz-secret-zzz")
    settings = ServerSettings(mode="remote")
    c = TestClient(create_app(_runtime(engine, registry), settings=settings))
    hdr = {"Authorization": "Bearer zzz-secret-zzz"}
    for path in ("/api/config", "/api/status", "/api/health"):
        assert "zzz-secret-zzz" not in c.get(path, headers=hdr).text


# --------------------------------------------------------------------------- #
# CORS (only when explicitly configured)
# --------------------------------------------------------------------------- #
def test_cors_header_present_for_configured_origin(engine, registry):
    settings = ServerSettings(cors_origins=["https://app.example.com"])
    c = TestClient(create_app(_runtime(engine, registry), settings=settings))
    r = c.get("/api/health", headers={"Origin": "https://app.example.com"})
    assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_no_cors_header_when_not_configured(engine, registry):
    c = TestClient(create_app(_runtime(engine, registry)))
    r = c.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers


# --------------------------------------------------------------------------- #
# Config + service diagnostics
# --------------------------------------------------------------------------- #
def test_config_endpoint_reports_mode(engine, registry):
    settings = ServerSettings(mode="local", port=8123)
    c = TestClient(create_app(_runtime(engine, registry), settings=settings))
    body = c.get("/api/config").json()
    assert body["mode"] == "local" and body["port"] == 8123
    assert body["auth_enabled"] is False


def test_service_endpoint_reports_stats(engine, registry):
    rt = _runtime(engine, registry)
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start(); svc.tick()
    c = TestClient(create_app(rt, service=svc))
    body = c.get("/api/service").json()
    assert body["tick_count"] == 1
    assert "cycles_run" in body and "duplicates_skipped" in body


def test_service_endpoint_without_service(engine, registry):
    c = TestClient(create_app(_runtime(engine, registry)))
    body = c.get("/api/service").json()
    assert body["running"] is False


# --------------------------------------------------------------------------- #
# Terminal UI assets
# --------------------------------------------------------------------------- #
def test_chart_library_is_served_without_a_token(engine, registry, monkeypatch):
    """The terminal loads its chart engine with a plain <script src>, which
    cannot send the bearer token. If /static ever stopped being auth-exempt the
    chart would die silently in remote mode — so pin it."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "abc")
    c = TestClient(create_app(_runtime(engine, registry),
                              settings=ServerSettings(mode="remote")))
    r = c.get("/static/vendor/lightweight-charts.standalone.production.js")
    assert r.status_code == 200
    assert "Lightweight Charts" in r.text[:400]
    assert c.get("/static/vendor/lightweight-charts.LICENSE.txt").status_code == 200
    # the practice tab's TradingView-style chart (KLineChart, Apache-2.0)
    r = c.get("/static/vendor/klinecharts.min.js")
    assert r.status_code == 200 and "klinecharts" in r.text[:2000]
    assert c.get("/static/vendor/klinecharts.LICENSE.txt").status_code == 200
    assert c.get("/static/vendor/klinecharts.NOTICE.txt").status_code == 200


def test_index_references_the_vendored_chart_library(engine, registry):
    html = TestClient(create_app(_runtime(engine, registry))).get("/").text
    assert "/static/vendor/lightweight-charts.standalone.production.js" in html
    assert "/static/vendor/klinecharts.min.js" in html
    # served locally, never from a third-party CDN
    assert "unpkg.com" not in html and "cdn." not in html


def test_ghost_market_exposes_the_plain_language_read(engine, registry):
    rt = _runtime(engine, registry)
    rt.ghost_market_read["ES"] = ["1H alcista.", "Régimen: rango."]
    body = TestClient(create_app(rt)).get("/api/ghost/market/ES").json()
    assert body["market_read"] == ["1H alcista.", "Régimen: rango."]


def test_ghost_market_read_is_empty_before_any_cycle(engine, registry):
    body = TestClient(create_app(_runtime(engine, registry))).get("/api/ghost/market/ES").json()
    assert body["market_read"] == []
