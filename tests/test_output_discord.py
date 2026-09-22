"""Tests for the Discord output adapter (Phase 5.5). No real network calls:
the transport is always a mock/fake. Never assert on/print a real webhook."""
from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.features.market_structure.models import Direction
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.output.discord import DiscordOutputAdapter, build_discord_payload
from signal_engine.output.events import OutputEvent, dedup_key_for
from signal_engine.setups.models import LevelRef, SetupCandidate, SetupStatus

TZ = "America/New_York"
FAKE_WEBHOOK = "https://discord.example.invalid/webhook/does-not-exist"  # never a real URL


def _candidate(**overrides) -> SetupCandidate:
    ts = pd.Timestamp("2026-06-15 10:05", tz=TZ)
    base = dict(
        setup_name="orb", instrument="ES", direction=Direction.BULLISH,
        timestamp=ts, timeframe="5m", entry=5013.84, stop_loss=5006.46,
        take_profits=[5019.15, 5024.46], risk_reward=[0.72, 1.44],
        reasons=["Opening range fully formed", "Closed above OR high"],
        invalidation_conditions=["Close back inside the opening range"],
        invalidation_price=5006.46,
        valid_until=ts + pd.Timedelta(minutes=60),
        market_context={"session": "RTH", "or_high": 5011.77, "or_low": 5006.46},
        referenced=[LevelRef("opening_range_high", None, 5011.77),
                    LevelRef("opening_range_low", None, 5006.46)],
        status=SetupStatus.VALID,
    )
    base.update(overrides)
    return SetupCandidate(**base)


class RecordingTransport:
    """Fake HTTP transport: records calls, returns a scripted status/exception
    sequence. Never touches the network."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, data, timeout):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _adapter(config=None, transport=None, webhook_url=FAKE_WEBHOOK):
    cfg = config or DiscordOutputConfig(enabled=True)
    return DiscordOutputAdapter(cfg, webhook_url=webhook_url, transport=transport,
                                sleep=lambda s: None)


# ------------------------- 1. Discord disabled --------------------------- #
def test_disabled_never_calls_transport():
    transport = RecordingTransport([200])
    adapter = _adapter(config=DiscordOutputConfig(enabled=False), transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is False
    assert transport.calls == []


# ------------------------- 2. enabled + valid webhook --------------------- #
def test_enabled_with_valid_webhook_sends_once():
    transport = RecordingTransport([200])
    adapter = _adapter(transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is True
    assert len(transport.calls) == 1


# ------------------------- 3. webhook not configured ----------------------- #
def test_webhook_not_configured_skips(caplog):
    transport = RecordingTransport([200])
    adapter = _adapter(transport=transport, webhook_url=None)
    event = OutputEvent.from_setup_candidate(_candidate())
    with caplog.at_level("WARNING"):
        result = adapter.emit(event)
    assert result is False
    assert transport.calls == []


# ------------------------- 4. HTTP error ----------------------------------- #
def test_http_error_gives_up_after_retries():
    transport = RecordingTransport([500, 500, 500])  # never recovers
    adapter = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=2),
                       transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is False
    assert len(transport.calls) == 3   # 1 initial + 2 retries


def test_non_retryable_http_error_stops_immediately():
    transport = RecordingTransport([400])
    adapter = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=2),
                       transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is False
    assert len(transport.calls) == 1   # no retry on a non-retryable 4xx


# ------------------------- 5. timeout -------------------------------------- #
def test_timeout_is_handled_and_does_not_raise():
    transport = RecordingTransport([TimeoutError("timed out"), TimeoutError("timed out")])
    adapter = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=1),
                       transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is False
    assert len(transport.calls) == 2


# ------------------------- 6. retry then success ---------------------------- #
def test_retry_then_success():
    transport = RecordingTransport([503, 200])
    adapter = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=2),
                       transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is True
    assert len(transport.calls) == 2


# ------------------------- 7. deduplication -------------------------------- #
def test_deduplication_prevents_second_send():
    transport = RecordingTransport([200, 200])
    adapter = _adapter(transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is True
    assert adapter.emit(event) is False       # same candidate -> deduped
    assert len(transport.calls) == 1


def test_deduplication_disabled_sends_again():
    transport = RecordingTransport([200, 200])
    adapter = _adapter(config=DiscordOutputConfig(enabled=True, deduplication_enabled=False),
                       transport=transport)
    event = OutputEvent.from_setup_candidate(_candidate())
    assert adapter.emit(event) is True
    assert adapter.emit(event) is True
    assert len(transport.calls) == 2


def test_dedup_key_deterministic_no_randomness():
    c = _candidate()
    k1 = dedup_key_for(c)
    k2 = dedup_key_for(c)
    assert k1 == k2
    # a candidate that is byte-identical up to the compared fields -> same key
    c2 = _candidate(reasons=["different rationale text, should not affect key"])
    assert dedup_key_for(c2) == k1


# ------------------------- 12. different setups differ --------------------- #
def test_different_setups_generate_different_events():
    c1 = _candidate(setup_name="orb")
    c2 = _candidate(setup_name="vwap_reclaim")
    c3 = _candidate(direction=Direction.BEARISH)
    c4 = _candidate(entry=5013.90)
    c5 = _candidate(timestamp=pd.Timestamp("2026-06-15 10:10", tz=TZ))
    keys = {dedup_key_for(c) for c in (c1, c2, c3, c4, c5)}
    assert len(keys) == 5


# ------------------------- 8. payload correctness --------------------------- #
def test_payload_structure():
    event = OutputEvent.from_setup_candidate(_candidate())
    payload = build_discord_payload(event)
    assert "embeds" in payload and len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == "🟡 SETUP CANDIDATE"
    assert "Setup Candidate — not a final trading signal." in embed["description"]
    assert isinstance(embed["fields"], list) and len(embed["fields"]) > 0


# ------------------------- 9. all key fields present ------------------------ #
def test_payload_contains_all_candidate_fields():
    c = _candidate()
    event = OutputEvent.from_setup_candidate(c)
    payload = build_discord_payload(event)
    field_names = {f["name"] for f in payload["embeds"][0]["fields"]}
    expected = {
        "Instrument", "Setup", "Bias", "Timestamp", "Timeframe", "Entry",
        "Stop Loss", "Take Profit 1", "Take Profit 2", "R:R", "Market context",
        "Reasons", "Referenced liquidity / structure", "Invalidation",
        "Expiration", "Status",
    }
    assert expected.issubset(field_names)
    field_map = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
    assert field_map["Instrument"] == "ES"
    assert field_map["Setup"] == "orb"
    assert field_map["Bias"] == "BULLISH"           # not LONG/SHORT
    assert field_map["Entry"] == "5013.84"
    assert field_map["Stop Loss"] == "5006.46"
    assert field_map["Take Profit 1"] == "5019.15"
    assert field_map["Take Profit 2"] == "5024.46"
    assert field_map["Status"] == "VALID"


def test_payload_omits_tp2_when_single_target():
    c = _candidate(take_profits=[5019.15], risk_reward=[0.72])
    payload = build_discord_payload(OutputEvent.from_setup_candidate(c))
    names = {f["name"] for f in payload["embeds"][0]["fields"]}
    assert "Take Profit 1" in names
    assert "Take Profit 2" not in names


def test_never_presented_as_long_short():
    """The message must never present a definitive LONG/SHORT order: the Bias
    field uses BULLISH/BEARISH, and the only appearance of LONG/SHORT text is
    the disclaimer explicitly denying it's a final recommendation."""
    payload = build_discord_payload(OutputEvent.from_setup_candidate(_candidate()))
    embed = payload["embeds"][0]
    field_map = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_map["Bias"] in ("BULLISH", "BEARISH")
    assert "not a final long/short recommendation" in embed["description"].lower()
    # no field anywhere claims a definitive LONG/SHORT signal
    assert all(v not in ("LONG", "SHORT") for v in field_map.values())


# ------------------------- 10. webhook never in logs/errors ----------------- #
def test_webhook_never_appears_in_logs_on_http_error(caplog):
    transport = RecordingTransport([500, 500, 500])
    adapter = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=2),
                       transport=transport)
    with caplog.at_level("WARNING"):
        adapter.emit(OutputEvent.from_setup_candidate(_candidate()))
    assert FAKE_WEBHOOK not in caplog.text


def test_webhook_never_appears_in_logs_on_exception(caplog):
    class Boom(Exception):
        pass

    def bad_transport(url, data, timeout):
        raise Boom(f"connection failed to {url}")   # exception message could leak

    adapter = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=0),
                       transport=bad_transport)
    with caplog.at_level("WARNING"):
        adapter.emit(OutputEvent.from_setup_candidate(_candidate()))
    assert FAKE_WEBHOOK not in caplog.text


def test_webhook_never_appears_in_repr():
    """repr()/str() of the adapter (what would show up in a traceback or a
    '%r' debug log of the object) must never leak the webhook, even though the
    adapter necessarily holds it internally to perform the POST."""
    adapter = _adapter()
    assert FAKE_WEBHOOK not in repr(adapter)
    assert FAKE_WEBHOOK not in str(adapter)


def test_env_var_never_hardcoded_and_config_stores_only_var_name():
    cfg = DiscordOutputConfig(enabled=True)
    assert cfg.webhook_env == "DISCORD_WEBHOOK_URL"
    # config object itself carries no webhook value field
    assert not hasattr(cfg, "webhook_url")


def test_adapter_reads_webhook_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    adapter = DiscordOutputAdapter(DiscordOutputConfig(enabled=True))
    assert adapter.configured is True


def test_adapter_unconfigured_when_env_missing(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    adapter = DiscordOutputAdapter(DiscordOutputConfig(enabled=True))
    assert adapter.configured is False


# ------------------------- 11. failure never breaks pipeline ---------------- #
def test_dispatcher_failure_does_not_raise():
    transport = RecordingTransport([500, 500, 500])
    discord = _adapter(config=DiscordOutputConfig(enabled=True, retry_count=2),
                       transport=transport)
    dispatcher = OutputDispatcher([discord])
    # must not raise even though every send fails
    dispatcher.publish_candidate(_candidate())


def test_dispatcher_raising_adapter_does_not_propagate():
    class ExplodingAdapter:
        name = "boom"
        def emit(self, event):
            raise RuntimeError("adapter exploded")

    dispatcher = OutputDispatcher([ExplodingAdapter()])
    dispatcher.publish_candidate(_candidate())   # must not raise


def test_dispatcher_from_config_disabled_builds_no_adapter():
    from signal_engine.config.schema import EngineConfig
    cfg = EngineConfig.load()
    cfg.output.discord.enabled = False   # the state under test, not the shipped default
    dispatcher = OutputDispatcher.from_config(cfg)
    assert dispatcher.adapters == []
    dispatcher.publish_candidate(_candidate())    # no-op, must not raise
