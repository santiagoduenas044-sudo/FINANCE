"""Tests for Phase 11 (Delayed Real Market Data):
- YahooDelayedAdapter against an injected fake transport (no real network —
  same pattern as DiscordOutputAdapter's injectable transport).
- LiveFeed.health() reporting DataFeedStatus.DELAYED for a delayed source.
- BotRuntime's on-disk signal_log_path audit trail (survives "restart").
- GET /api/signals/history.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.data.ingestion import normalize
from signal_engine.live.feed import LiveFeed
from signal_engine.live.models import DataFeedStatus
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.live.yahoo_adapter import YahooDelayedAdapter
from signal_engine.runtime.engine import BotRuntime, read_signal_log
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from signal_engine.signals.models import Signal, SignalDirection

SAMPLES = Path(__file__).resolve().parent / "data_samples"


class FixedClock:
    def __init__(self, start=None):
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self._now

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)


def _chart_payload(ticker: str, base_epoch: int, closes: list[float]) -> bytes:
    """Build a minimal Yahoo chart-API JSON payload for `len(closes)` 1m bars,
    plus one trailing still-forming bar (dropped by the adapter)."""
    n = len(closes) + 1
    timestamps = [base_epoch + 60 * i for i in range(n)]
    opens = closes[:1] + closes[:-1] if closes else []
    opens = [closes[0]] + closes[:-1] if closes else []
    opens = opens + [closes[-1]] if closes else [0.0]   # forming bar's open == prior close
    highs = [c + 0.5 for c in closes] + [closes[-1] + 0.5] if closes else [0.5]
    lows = [c - 0.5 for c in closes] + [closes[-1] - 0.5] if closes else [-0.5]
    all_closes = closes + [closes[-1]] if closes else [0.0]
    volumes = [100.0] * n
    payload = {
        "chart": {
            "result": [{
                "meta": {"symbol": ticker},
                "timestamp": timestamps,
                "indicators": {"quote": [{
                    "open": opens, "high": highs, "low": lows,
                    "close": all_closes, "volume": volumes,
                }]},
            }],
            "error": None,
        }
    }
    return json.dumps(payload).encode("utf-8")


# --------------------------------------------------------------------------- #
# YahooDelayedAdapter
# --------------------------------------------------------------------------- #
def test_yahoo_adapter_is_delayed_by_contract():
    assert YahooDelayedAdapter.is_delayed is True


def test_yahoo_adapter_connect_and_poll_returns_closed_bars_only():
    base = 1_800_000_000  # arbitrary fixed epoch
    calls = []

    def fake_transport(ticker, timeout):
        calls.append(ticker)
        return _chart_payload(ticker, base, [5000.0, 5001.5, 5003.0])

    adapter = YahooDelayedAdapter(transport=fake_transport)
    adapter.connect()
    assert adapter.is_connected

    bars = adapter.poll("ES")
    assert [b.close for b in bars] == [5000.0, 5001.5, 5003.0]
    assert all(b.timestamp.tzinfo is not None for b in bars)
    assert calls[0] == "ES=F"      # correct ticker mapping, no TradingView anywhere


def test_yahoo_adapter_does_not_resend_already_emitted_bars():
    base = 1_800_000_000

    def fake_transport(ticker, timeout):
        return _chart_payload(ticker, base, [5000.0, 5001.0])

    adapter = YahooDelayedAdapter(transport=fake_transport)
    adapter.connect()
    first = adapter.poll("ES")
    second = adapter.poll("ES")     # same underlying payload again
    assert len(first) == 2
    assert second == []             # nothing new -> nothing re-sent


def test_yahoo_adapter_raises_connection_error_on_transport_failure():
    def failing_transport(ticker, timeout):
        raise OSError("network unreachable")

    adapter = YahooDelayedAdapter(transport=failing_transport)
    with pytest.raises(ConnectionError):
        adapter.connect()
    assert not adapter.is_connected


def test_yahoo_adapter_raises_connection_error_on_malformed_response():
    def bad_transport(ticker, timeout):
        return b"not json"

    adapter = YahooDelayedAdapter(transport=bad_transport)
    with pytest.raises(ConnectionError):
        adapter.connect()


def test_yahoo_adapter_poll_before_connect_raises():
    adapter = YahooDelayedAdapter(transport=lambda t, to: _chart_payload(t, 0, [1.0]))
    with pytest.raises(ConnectionError):
        adapter.poll("ES")


# --------------------------------------------------------------------------- #
# LiveFeed DELAYED status
# --------------------------------------------------------------------------- #
def _normalized_es_bars(engine):
    provider = CsvProvider(SAMPLES)
    raw = provider.get_raw_bars("ES", "1m", contract="ESM26")
    return normalize(raw, engine.cfg.data, "1m")


def test_live_feed_reports_delayed_status_for_a_delayed_adapter(engine):
    base = 1_800_000_000

    def fake_transport(ticker, timeout):
        return _chart_payload(ticker, base, [5000.0, 5001.0])

    adapter = YahooDelayedAdapter(transport=fake_transport, timezone=engine.cfg.data.timezone)
    clock = FixedClock()
    feed = LiveFeed(adapter, "ES", engine.cfg, now_fn=clock, sleep_fn=lambda s: None)

    feed.poll()
    assert feed.health().status is DataFeedStatus.DELAYED


def test_live_feed_still_reports_live_for_a_non_delayed_adapter(engine):
    bars = _normalized_es_bars(engine)
    adapter = SimulatedLiveAdapter(bars, symbol="ES", bars_per_poll=5)
    clock = FixedClock()
    feed = LiveFeed(adapter, "ES", engine.cfg, now_fn=clock, sleep_fn=lambda s: None)

    feed.poll()
    assert feed.health().status is DataFeedStatus.LIVE   # unchanged Phase 10 behavior


def test_live_feed_delayed_source_still_goes_stale_when_data_stops_arriving(engine):
    base = 1_800_000_000
    payload = _chart_payload("ES=F", base, [5000.0])

    def fake_transport(ticker, timeout):
        return payload

    adapter = YahooDelayedAdapter(transport=fake_transport, timezone=engine.cfg.data.timezone)
    clock = FixedClock()
    feed = LiveFeed(adapter, "ES", engine.cfg, now_fn=clock, sleep_fn=lambda s: None)
    feed.poll()
    assert feed.health().status is DataFeedStatus.DELAYED

    clock.advance(seconds=engine.cfg.live.stale_after_seconds + 1)
    assert feed.health().status is DataFeedStatus.STALE


# --------------------------------------------------------------------------- #
# Persistent signal log
# --------------------------------------------------------------------------- #
def _make_signal(ts) -> Signal:
    return Signal(
        signal_id="sig-test-1", instrument="ES", setup="vwap_reclaim",
        direction=SignalDirection.LONG, timestamp=ts, timeframe="5m",
        entry=5000.0, stop=4995.0, take_profits=[5010.0], risk_reward=[2.0],
        reasons=["test fixture"],
    )


def test_signal_is_persisted_to_disk_and_survives_a_fresh_runtime(tmp_path, engine, registry):
    log_path = tmp_path / "signal_log.jsonl"
    runtime = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                        SignalEngine(engine.cfg.signals), registry,
                        signal_log_path=log_path)

    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    runtime._handle_signal(_make_signal(ts))

    assert log_path.exists()
    entries = read_signal_log(log_path)
    assert len(entries) == 1
    assert entries[0]["instrument"] == "ES"
    assert entries[0]["direction"] == "LONG"

    # A brand new BotRuntime (simulating a dashboard restart) pointed at the
    # same path can still read the history — that's the whole point.
    fresh_runtime = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                               SignalEngine(engine.cfg.signals), registry,
                               signal_log_path=log_path)
    assert read_signal_log(fresh_runtime.signal_log_path) == entries


def test_read_signal_log_returns_empty_list_when_file_does_not_exist(tmp_path):
    assert read_signal_log(tmp_path / "does_not_exist.jsonl") == []


def test_runtime_without_signal_log_path_never_touches_disk(tmp_path, engine, registry):
    runtime = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                        SignalEngine(engine.cfg.signals), registry)
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    runtime._handle_signal(_make_signal(ts))   # must not raise, must not create files
    assert list(tmp_path.iterdir()) == []


def test_api_signals_history_reports_not_persisted_when_no_log_configured(engine, registry):
    runtime = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                        SignalEngine(engine.cfg.signals), registry)
    client = TestClient(create_app(runtime))
    resp = client.get("/api/signals/history")
    assert resp.status_code == 200
    assert resp.json() == {"signals": [], "persisted": False}


def test_api_signals_history_returns_persisted_signals(tmp_path, engine, registry):
    log_path = tmp_path / "signal_log.jsonl"
    runtime = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                        SignalEngine(engine.cfg.signals), registry,
                        signal_log_path=log_path)
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    runtime._handle_signal(_make_signal(ts))

    client = TestClient(create_app(runtime))
    resp = client.get("/api/signals/history")
    body = resp.json()
    assert body["persisted"] is True
    assert len(body["signals"]) == 1
    assert body["signals"][0]["instrument"] == "ES"
