"""Tests for Live Market Data (Phase 10): LiveDataAdapter -> LiveFeed ->
BotRuntime -> Dashboard/Discord. No internet — SimulatedLiveAdapter replays
sample historical data as if it were arriving live, with deterministic fault
injection for disconnect/reconnect scenarios.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.ingestion import normalize
from signal_engine.data.models import CLOSE, HIGH, LOW, OPEN, SESSION, TRADING_DATE, VOLUME
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.output.discord import DiscordOutputAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.live.feed import LiveFeed
from signal_engine.live.models import DataFeedStatus, LiveBar
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

TZ = "America/New_York"
SAMPLES = Path(__file__).resolve().parent / "data_samples"
FAKE_WEBHOOK = "https://discord.example.invalid/webhook/does-not-exist"


class FixedClock:
    def __init__(self, start=None):
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self._now

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)


def _normalized_es_bars(engine):
    provider = CsvProvider(SAMPLES)
    raw = provider.get_raw_bars("ES", "1m", contract="ESM26")
    return normalize(raw, engine.cfg.data, "1m")


def _feed(engine, bars=None, bars_per_poll=20, now_fn=None):
    bars = bars if bars is not None else _normalized_es_bars(engine)
    adapter = SimulatedLiveAdapter(bars, symbol="ES", bars_per_poll=bars_per_poll)
    return LiveFeed(adapter, "ES", engine.cfg, now_fn=now_fn or FixedClock(),
                    sleep_fn=lambda s: None), adapter


# --------------------------------------------------------------------------- #
# valid data / bar building
# --------------------------------------------------------------------------- #
def test_poll_returns_closed_bars_in_canonical_shape(engine):
    feed, adapter = _feed(engine)
    bars = feed.poll()
    assert len(bars) == 20
    assert all(isinstance(b, LiveBar) for b in bars)
    assert all(b.timestamp.tzinfo is not None for b in bars)   # tz-aware, close-labeled


def test_get_tf_bars_matches_historical_schema(engine):
    feed, _ = _feed(engine)
    feed.poll()
    tf_bars = feed.get_tf_bars()
    assert set(tf_bars) == {"1m", "5m", "15m", "1H"}
    for tf, df in tf_bars.items():
        assert list(df.columns[:5]) == [OPEN, HIGH, LOW, CLOSE, VOLUME]
        assert SESSION in df.columns and TRADING_DATE in df.columns
        assert df[[OPEN, HIGH, LOW, CLOSE, VOLUME]].dtypes.eq("float64").all()


def test_5m_15m_1h_bars_are_correctly_aggregated(engine):
    feed, _ = _feed(engine, bars_per_poll=60)
    feed.poll()
    tf_bars = feed.get_tf_bars()
    # 60 1m bars -> at least a few 5m/15m and at least one 1H bar
    assert len(tf_bars["5m"]) >= 10
    assert len(tf_bars["15m"]) >= 3
    assert len(tf_bars["1H"]) >= 1
    # OHLC integrity preserved by aggregation
    for tf in ("5m", "15m", "1H"):
        df = tf_bars[tf]
        assert (df[HIGH] >= df[LOW]).all()
        assert (df[HIGH] >= df[[OPEN, CLOSE]].max(axis=1)).all()


def test_timestamps_are_close_labeled_and_tz_aware(engine):
    feed, _ = _feed(engine)
    feed.poll()
    df = feed.get_tf_bars()["1m"]
    assert str(df.index.tz) == TZ
    # matches the historical normalized source exactly for the same rows
    hist = _normalized_es_bars(engine)
    assert df.index[0] == hist.index[0]


# --------------------------------------------------------------------------- #
# duplicate / incomplete data
# --------------------------------------------------------------------------- #
def test_duplicate_bar_delivery_keeps_latest_value(engine):
    feed, adapter = _feed(engine, bars_per_poll=5)
    feed.poll()
    first_ts = feed._buffer.index[0]
    # re-deliver the same timestamp with a different close price
    feed._ingest([LiveBar("ES", first_ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=9.0)])
    assert not feed._buffer.index.duplicated().any()
    assert feed._buffer.loc[first_ts, "close"] == 1.5   # latest value wins


def test_incomplete_or_invalid_bars_are_dropped_not_corrupting_buffer(engine):
    feed, _ = _feed(engine, bars_per_poll=3)
    feed.poll()
    before = len(feed._buffer)
    ts = feed._buffer.index[-1] + pd.Timedelta(minutes=1)
    bad = LiveBar("ES", ts, open=100.0, high=90.0, low=99.0, close=100.0, volume=10.0)  # high<low
    good = LiveBar("ES", ts + pd.Timedelta(minutes=1), open=100.0, high=105.0, low=99.0,
                  close=101.0, volume=10.0)
    feed._ingest([bad, good])
    assert len(feed._buffer) == before + 1        # only the good bar was added
    assert feed.last_validation_report.n_dropped == 1
    assert not feed._buffer[[OPEN, HIGH, LOW, CLOSE]].isna().any().any()


def test_negative_volume_bar_is_dropped(engine):
    feed, _ = _feed(engine, bars_per_poll=1)
    feed.poll()
    ts = feed._buffer.index[-1] + pd.Timedelta(minutes=1)
    bad = LiveBar("ES", ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=-1.0)
    before = len(feed._buffer)
    feed._ingest([bad])
    assert len(feed._buffer) == before


# --------------------------------------------------------------------------- #
# disconnection / reconnection / staleness
# --------------------------------------------------------------------------- #
def test_health_disconnected_before_first_poll(engine):
    feed, _ = _feed(engine)
    assert feed.health().status is DataFeedStatus.DISCONNECTED


def test_health_live_after_successful_poll(engine):
    feed, _ = _feed(engine)
    feed.poll()
    assert feed.health().status is DataFeedStatus.LIVE


def test_health_stale_when_no_fresh_data_for_too_long(engine):
    clock = FixedClock()
    feed, _ = _feed(engine, now_fn=clock)
    feed.poll()
    assert feed.health().status is DataFeedStatus.LIVE
    clock.advance(seconds=engine.cfg.live.stale_after_seconds + 1)
    assert feed.health().status is DataFeedStatus.STALE


def test_disconnect_then_reconnect_recovers(engine):
    feed, adapter = _feed(engine, bars_per_poll=5)
    feed.poll()
    assert feed.health().status is DataFeedStatus.LIVE

    adapter.simulate_disconnect()
    bars = feed.poll()
    assert bars == []
    assert feed.health().status is DataFeedStatus.DISCONNECTED
    assert feed.health().reconnect_attempts == engine.cfg.live.reconnect_max_attempts

    adapter.simulate_reconnect()
    bars2 = feed.poll()
    assert len(bars2) == 5
    assert feed.health().status is DataFeedStatus.LIVE
    assert feed.health().reconnect_attempts == 0


def test_disconnected_feed_does_not_crash_or_raise(engine):
    feed, adapter = _feed(engine)
    adapter.simulate_disconnect()
    # poll() while never having connected at all
    assert feed.poll() == []
    assert feed.health().status is DataFeedStatus.DISCONNECTED


def test_last_error_cleared_after_successful_recovery(engine):
    feed, adapter = _feed(engine, bars_per_poll=3)
    feed.poll()
    adapter.simulate_disconnect()
    feed.poll()
    assert feed.health().detail != ""
    adapter.simulate_reconnect()
    feed.poll()
    assert feed.health().detail == ""


# --------------------------------------------------------------------------- #
# no-lookahead
# --------------------------------------------------------------------------- #
def test_earlier_decisions_unaffected_by_later_arriving_bars(engine):
    """Building an AnalysisContext at N, then polling MORE bars in, must not
    change what a context built at that same N (using the buffer as it was)
    looked like — the classic no-lookahead guarantee, now driven by a live,
    growing buffer instead of a static historical file."""
    feed, _ = _feed(engine, bars_per_poll=30)
    feed.poll()
    tf_bars_before = feed.get_tf_bars()
    N = tf_bars_before["5m"].index[len(tf_bars_before["5m"]) // 2]
    ctx_before = engine.analysis_context("ES", N, tf_bars_before)

    feed.poll()   # more live bars arrive
    feed.poll()
    tf_bars_after = feed.get_tf_bars()
    ctx_after = engine.analysis_context("ES", N, tf_bars_after)

    pd.testing.assert_frame_equal(
        ctx_before.execution.bars[[OPEN, HIGH, LOW, CLOSE]],
        ctx_after.execution.bars[[OPEN, HIGH, LOW, CLOSE]],
    )


# --------------------------------------------------------------------------- #
# Runtime integration
# --------------------------------------------------------------------------- #
def _runtime_with_feed(engine, dispatcher=None, bars_per_poll=20):
    registry = ContractRegistry.load()
    feed, adapter = _feed(engine, bars_per_poll=bars_per_poll)
    se = SetupEngine(engine.cfg, registry)
    sig_eng = SignalEngine(engine.cfg.signals)
    kwargs = {}
    if dispatcher is not None:
        kwargs["output_dispatcher"] = dispatcher
    rt = BotRuntime(engine, se, sig_eng, registry, live_feeds={"ES": feed},
                    now_fn=feed.now_fn, **kwargs)
    return rt, feed, adapter


def test_run_cycle_pulls_from_live_feed_when_no_tf_bars_given(engine):
    rt, feed, _ = _runtime_with_feed(engine)
    rt.start()
    result = rt.run_cycle("ES")
    assert result.ran is True
    assert rt.data_feed_status["ES"] is DataFeedStatus.LIVE


def test_dashboard_shows_disconnected_status_via_runtime(engine):
    rt, feed, adapter = _runtime_with_feed(engine)
    rt.start()
    rt.run_cycle("ES")
    adapter.simulate_disconnect()
    rt.run_cycle("ES")
    status = rt.to_status_dict()
    assert status["data_feed_status"]["ES"] == "DISCONNECTED"
    assert status["components"]["data"]["status"] == "DOWN"
    assert rt.status.value == "ONLINE"        # a feed hiccup is not a pipeline ERROR


def test_dashboard_shows_stale_status(engine):
    """Staleness means no NEW data has arrived recently — exhaust the feed's
    available bars first, then advancing the clock with no new bars to
    deliver must show STALE (not LIVE just because we happened to poll)."""
    clock = FixedClock()
    rt, feed, adapter = _runtime_with_feed(engine, bars_per_poll=100_000)
    rt.now_fn = clock
    feed.now_fn = clock
    rt.start()
    rt.run_cycle("ES")
    assert adapter.exhausted
    assert rt.to_status_dict()["data_feed_status"]["ES"] == "LIVE"

    clock.advance(seconds=engine.cfg.live.stale_after_seconds + 1)
    rt.run_cycle("ES")   # polls again, but adapter has nothing new to deliver
    assert rt.to_status_dict()["data_feed_status"]["ES"] == "STALE"


def test_no_data_source_configured_is_refused_cleanly(engine):
    registry = ContractRegistry.load()
    se = SetupEngine(engine.cfg, registry)
    sig_eng = SignalEngine(engine.cfg.signals)
    rt = BotRuntime(engine, se, sig_eng, registry)   # no live_feeds at all
    rt.start()
    result = rt.run_cycle("ES")     # tf_bars omitted -> should look for a live feed
    assert result.ran is False
    assert "no data source" in result.reason


def test_pausing_stops_live_pipeline_too(engine):
    rt, feed, _ = _runtime_with_feed(engine)
    rt.start()
    rt.run_cycle("ES")
    rt.pause()
    result = rt.run_cycle("ES")
    assert result.ran is False
    assert "PAUSED" in result.reason


# --------------------------------------------------------------------------- #
# full pipeline through to Discord (mocked transport, no real network)
# --------------------------------------------------------------------------- #
class RecordingTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, data, timeout):
        self.calls.append(data)
        return 204


def test_full_pipeline_reaches_discord_with_real_signals(engine):
    transport = RecordingTransport()
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True, deduplication_enabled=False),
                                   webhook_url=FAKE_WEBHOOK, transport=transport,
                                   sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt, feed, _ = _runtime_with_feed(engine, dispatcher=dispatcher, bars_per_poll=60)
    rt.start()   # sends BOT_STARTED notification

    for _ in range(6):
        rt.run_cycle("ES")

    assert rt.trades_today > 0
    # BOT_STARTED + at least one real Signal notification
    assert len(transport.calls) >= 2
    import json
    kinds = [json.loads(c)["embeds"][0]["title"] for c in transport.calls]
    assert any("SIGNAL" in k for k in kinds)


def test_full_pipeline_through_dashboard_api(engine):
    rt, feed, _ = _runtime_with_feed(engine, bars_per_poll=60)
    app = create_app(rt)
    client = TestClient(app)

    client.post("/api/control/start")
    for _ in range(6):
        rt.run_cycle("ES")

    status = client.get("/api/status").json()
    assert status["status"] == "ONLINE"
    assert status["data_feed_status"]["ES"] == "LIVE"
    signals = client.get("/api/signals/recent").json()["signals"]
    assert isinstance(signals, list)


def test_webhook_never_appears_in_live_pipeline_outputs(engine):
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True),
                                   webhook_url=FAKE_WEBHOOK,
                                   transport=RecordingTransport(), sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt, feed, _ = _runtime_with_feed(engine, dispatcher=dispatcher, bars_per_poll=30)
    rt.start()
    rt.run_cycle("ES")
    assert FAKE_WEBHOOK not in str(rt.to_status_dict())
    assert FAKE_WEBHOOK not in repr(discord)
