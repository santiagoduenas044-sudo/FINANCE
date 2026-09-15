"""Tests for Phase 12 (Interactive chart + premium dashboard).

Covered:
  * The read-only chart snapshot (`signal_engine.api.chart.build_chart_data`)
    reuses the real engines, is no-lookahead, and never runs the trading
    pipeline or mutates runtime trading state.
  * New API surface: /api/symbols, /api/chart/{symbol}, /api/signals/detail/{id}.
  * Signal outcome classification (WIN / LOSS / ACTIVE / INVALIDATED).
  * The enriched, JSON-safe signal record persists the full detail the
    dashboard needs and never leaks secrets.

Everything uses SimulatedLiveAdapter over the bundled ES sample data — no real
network, no real Discord — and the protected engines (Data / Market Analysis /
Setup / Signal / Risk) are only *called*, never modified.
"""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.api.chart import build_chart_data, _signal_status
from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.output.discord import DiscordOutputAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.runtime.engine import BotRuntime, _record_to_dict
from signal_engine.setups.engine import SetupEngine
from signal_engine.setups.models import LevelRef, risk_reward
from signal_engine.signals.engine import SignalEngine
from signal_engine.signals.models import Signal, SignalDirection, compute_signal_id

SAMPLES = Path(__file__).resolve().parent / "data_samples"
FAKE_WEBHOOK = "https://discord.example.invalid/webhook/does-not-exist"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _es_feed(engine):
    provider = CsvProvider(SAMPLES)
    raw = provider.get_raw_bars("ES", "1m", contract="ESM26")
    hist = normalize(raw, engine.cfg.data, "1m")
    adapter = SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=100000)
    feed = LiveFeed(adapter, "ES", engine.cfg)
    feed.poll()  # fill the buffer once
    return feed


def _runtime_with_feed(engine, registry, log_path=None):
    feed = _es_feed(engine)
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    live_feeds={"ES": feed}, signal_log_path=log_path)
    return rt


def _make_signal(ts, timeframe, entry=5000.0, stop=4995.0, tps=(5010.0,)):
    return Signal(
        signal_id=compute_signal_id("ES", "vwap_reclaim", SignalDirection.LONG,
                                    ts, timeframe, entry, stop),
        instrument="ES", setup="vwap_reclaim", direction=SignalDirection.LONG,
        timestamp=ts, timeframe=timeframe, entry=entry, stop=stop,
        take_profits=list(tps), risk_reward=risk_reward(entry, stop, list(tps)),
        reasons=["price reclaimed VWAP", "bullish BOS"],
        referenced=[LevelRef("vwap", ts, entry, "session VWAP")],
        invalidation_conditions=["close back below VWAP"], invalidation_price=stop,
        market_context={"session": "RTH", "vwap": entry},
    )


@pytest.fixture
def client_with_feed(engine, registry, tmp_path):
    rt = _runtime_with_feed(engine, registry, log_path=tmp_path / "signal_log.jsonl")
    return TestClient(create_app(rt)), rt


# --------------------------------------------------------------------------- #
# /api/symbols
# --------------------------------------------------------------------------- #
def test_symbols_lists_only_configured_feeds(client_with_feed):
    c, rt = client_with_feed
    body = c.get("/api/symbols").json()
    assert body["symbols"] == ["ES"]


def test_symbols_empty_when_no_feeds(engine, registry):
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry)
    c = TestClient(create_app(rt))
    assert c.get("/api/symbols").json() == {"symbols": []}


# --------------------------------------------------------------------------- #
# /api/chart/{symbol}
# --------------------------------------------------------------------------- #
def test_chart_returns_candles_and_indicators(client_with_feed):
    c, rt = client_with_feed
    d = c.get("/api/chart/ES?limit=120").json()
    assert d["available"] is True
    assert d["symbol"] == "ES"
    assert d["timeframe"] == rt.data_engine.cfg.timeframe_roles.execution
    assert 0 < len(d["candles"]) <= 120
    # OHLC integrity in the payload
    for cd in d["candles"]:
        assert cd["h"] >= cd["l"]
        assert cd["h"] >= cd["o"] and cd["h"] >= cd["c"]
        assert cd["l"] <= cd["o"] and cd["l"] <= cd["c"]
    # all three indicator series are present and produce points
    for key in ("vwap", "ema9", "ema20"):
        assert key in d["indicators"]
    assert d["indicators"]["ema9"], "EMA9 should have points on real data"
    # structure buckets always present
    for key in ("liquidity", "fvg", "events", "swings"):
        assert key in d["structure"]


def test_chart_unknown_symbol_is_404(client_with_feed):
    c, rt = client_with_feed
    assert c.get("/api/chart/NQ").status_code == 404
    assert c.get("/api/chart/DOGE").status_code == 404


def test_chart_limit_is_validated(client_with_feed):
    c, rt = client_with_feed
    assert c.get("/api/chart/ES?limit=5").status_code == 400
    assert c.get("/api/chart/ES?limit=9999").status_code == 400
    assert c.get("/api/chart/ES?limit=200").status_code == 200


def test_chart_is_no_lookahead(client_with_feed):
    """The last candle equals the decision time, and every indicator/structure
    point sits at or before it — the chart can never show a future bar."""
    c, rt = client_with_feed
    d = c.get("/api/chart/ES?limit=300").json()
    last_t = pd.Timestamp(d["last_bar_time"])
    assert pd.Timestamp(d["candles"][-1]["t"]) == last_t
    for series in d["indicators"].values():
        for p in series:
            assert pd.Timestamp(p["t"]) <= last_t
    for ev in d["structure"]["events"]:
        assert pd.Timestamp(ev["t"]) <= last_t


def test_chart_endpoint_does_not_run_trading_pipeline(client_with_feed):
    """Rendering the chart must be pure observability — no setups detected, no
    signals produced, no trade counter moved, status unchanged (OFFLINE)."""
    c, rt = client_with_feed
    trades_before = rt.trades_today
    signals_before = len(rt.recent_signals)
    status_before = rt.status
    c.get("/api/chart/ES?limit=200")
    c.get("/api/chart/ES?limit=200")
    assert rt.trades_today == trades_before
    assert len(rt.recent_signals) == signals_before
    assert rt.status == status_before


def test_chart_for_symbol_without_feed_is_graceful(engine, registry):
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry)
    d = build_chart_data(rt, "ES")
    assert d["available"] is False
    assert d["candles"] == []
    assert "no data feed" in d["reason"]


# --------------------------------------------------------------------------- #
# Signal overlay + status
# --------------------------------------------------------------------------- #
def test_signal_overlay_appears_on_chart_with_status_and_detail(client_with_feed):
    c, rt = client_with_feed
    exec_tf = rt.data_engine.cfg.timeframe_roles.execution
    bars = rt.live_feeds["ES"].get_tf_bars()[exec_tf]
    ts = bars.index[len(bars) // 2]
    price = float(bars.iloc[len(bars) // 2]["close"])
    rt.start()
    rt._handle_signal(_make_signal(ts, exec_tf, entry=price, stop=price - 5,
                                   tps=(price + 10,)))
    d = c.get("/api/chart/ES?limit=500").json()
    overlaid = [s for s in d["signals"] if s["kind"] == "SIGNAL"]
    assert len(overlaid) == 1
    sig = overlaid[0]
    assert sig["direction"] == "LONG"
    assert sig["status"] in {"WIN", "LOSS", "ACTIVE", "INVALIDATED"}
    # full detail is present for the click-through panel
    assert sig["reasons"] and sig["referenced"]
    assert "risk_reward" in sig and "invalidation_price" in sig


def test_signal_status_win_loss_active_invalidated():
    idx = pd.date_range("2026-06-15 10:00", periods=4, freq="5min", tz="America/New_York")
    win = pd.DataFrame({"high": [5002, 5006, 5011, 5012], "low": [4999, 5001, 5005, 5008]}, index=idx)
    assert _signal_status("LONG", 5000, 4995, 5010, 0.0, win) == "WIN"

    loss = pd.DataFrame({"high": [5002, 5001, 4999, 4997], "low": [4999, 4996, 4994, 4990]}, index=idx)
    assert _signal_status("LONG", 5000, 4995, 5010, 0.0, loss) == "LOSS"

    active = pd.DataFrame({"high": [5002, 5003, 5004, 5003], "low": [4999, 4998, 4999, 4998]}, index=idx)
    assert _signal_status("LONG", 5000, 4995, 5010, 0.0, active) == "ACTIVE"

    # distinct, tighter invalidation (4997) breached before the stop
    inval = pd.DataFrame({"high": [5002, 5001, 5000, 4999], "low": [4999, 4998, 4996, 4990]}, index=idx)
    assert _signal_status("LONG", 5000, 4995, 5010, 4997.0, inval) == "INVALIDATED"

    # no future bars -> still ACTIVE
    assert _signal_status("LONG", 5000, 4995, 5010, 0.0, pd.DataFrame()) == "ACTIVE"


def test_signal_status_conservative_on_intrabar_tie():
    """If a single bar spans both TP and stop, the outcome is LOSS (we can't
    know intrabar order) — matching the setup lifecycle convention."""
    idx = pd.date_range("2026-06-15 10:00", periods=1, freq="5min", tz="America/New_York")
    both = pd.DataFrame({"high": [5011], "low": [4994]}, index=idx)
    assert _signal_status("LONG", 5000, 4995, 5010, 0.0, both) == "LOSS"


def test_signal_status_short_direction():
    idx = pd.date_range("2026-06-15 10:00", periods=3, freq="5min", tz="America/New_York")
    win = pd.DataFrame({"high": [5001, 5000, 4999], "low": [4998, 4992, 4989]}, index=idx)
    assert _signal_status("SHORT", 5000, 5005, 4990, 0.0, win) == "WIN"
    loss = pd.DataFrame({"high": [5002, 5006, 5008], "low": [4999, 5001, 5004]}, index=idx)
    assert _signal_status("SHORT", 5000, 5005, 4990, 0.0, loss) == "LOSS"


# --------------------------------------------------------------------------- #
# /api/signals/detail/{id}
# --------------------------------------------------------------------------- #
def test_signal_detail_endpoint_returns_full_record(client_with_feed):
    c, rt = client_with_feed
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    sig = _make_signal(ts, "5m")
    rt._handle_signal(sig)
    d = c.get(f"/api/signals/detail/{sig.signal_id}").json()
    assert d["signal_id"] == sig.signal_id
    assert d["reasons"] == sig.reasons
    assert d["referenced"][0]["kind"] == "vwap"
    assert d["invalidation_conditions"] == sig.invalidation_conditions


def test_signal_detail_unknown_id_is_404(client_with_feed):
    c, rt = client_with_feed
    assert c.get("/api/signals/detail/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Enriched, JSON-safe record
# --------------------------------------------------------------------------- #
def test_record_to_dict_enriched_and_json_safe():
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    rec = _record_to_dict(_make_signal(ts, "5m"))
    for key in ("signal_id", "timeframe", "risk_reward", "reasons",
                "invalidation_conditions", "invalidation_price",
                "market_context", "referenced"):
        assert key in rec
    # referenced timestamps are serialised to ISO strings, not raw Timestamps
    assert isinstance(rec["referenced"][0]["timestamp"], str)
    # round-trips through JSON without error
    import json
    json.loads(json.dumps(rec))


def test_chart_and_detail_never_leak_webhook_secret(engine, registry, tmp_path):
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True),
                                   webhook_url=FAKE_WEBHOOK,
                                   transport=lambda url, data, timeout: 204,
                                   sleep=lambda s: None)
    rt = _runtime_with_feed(engine, registry, log_path=tmp_path / "log.jsonl")
    rt.output_dispatcher = OutputDispatcher([discord])
    c = TestClient(create_app(rt))
    assert FAKE_WEBHOOK not in c.get("/api/chart/ES").text
    assert FAKE_WEBHOOK not in c.get("/api/symbols").text
