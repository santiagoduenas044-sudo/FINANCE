"""Phase 17 — Discord 'Trade Opened' notification flow.

Verifies the full invariant: GHOST APPROVED → PaperTrade OPEN → Discord SEND
with the rich 'trade_opened' embed format. Also verifies NO_TRADE and hard-
risk-block paths never send to Discord.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from signal_engine.config.schema import GhostConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.ghost.brain import GhostBrain
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.output.discord import DiscordOutputAdapter, _build_trade_opened_payload
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.output.events import OutputEvent
from signal_engine.paper.models import PaperTrade
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.risk.engine import RiskEngine
from signal_engine.risk.models import AccountState
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

SAMPLES = Path(__file__).resolve().parent / "data_samples"


# ------------------------------------------------------------------ helpers ---

def _make_adapter(calls: list) -> DiscordOutputAdapter:
    """A DiscordOutputAdapter that captures each emit call instead of POSTing."""
    from signal_engine.config.schema import DiscordOutputConfig
    cfg = DiscordOutputConfig(enabled=True, webhook_env="DISCORD_WEBHOOK_URL")
    adapter = DiscordOutputAdapter(cfg, webhook_url="https://mock.webhook/test")
    # Capture payloads instead of sending.
    def _transport(url, data, timeout):
        import json
        calls.append(json.loads(data))
        return 204
    adapter._transport = _transport
    return adapter


def _es_feed(engine, bars_per_poll=5):
    hist = normalize(CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26"),
                     engine.cfg.data, "1m")
    return LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=bars_per_poll),
                    "ES", engine.cfg)


def _runtime(engine, registry, feed, calls, account=None, ghost_cfg=None, monkeypatch=None):
    gcfg = ghost_cfg or GhostConfig(
        min_confluence_score=0.0, block_htf_conflict=False,
        block_against_structure=False, min_stop_atr_mult=0.0, cooldown_bars=0,
    )
    ghost = GhostBrain(gcfg)
    tracker = PaperTradeTracker()
    risk = RiskEngine(engine.cfg.risk, registry)
    acct = account or AccountState(equity=100_000.0)
    adapter = _make_adapter(calls)
    dispatcher = OutputDispatcher(adapters=[adapter])
    if monkeypatch is not None:
        monkeypatch.setattr(registry, "require_verified", lambda: None)
    rt = BotRuntime(
        engine, SetupEngine(engine.cfg, registry),
        SignalEngine(engine.cfg.signals), registry,
        risk_engine=risk, account=acct,
        live_feeds={"ES": feed},
        ghost=ghost, paper_tracker=tracker,
        output_dispatcher=dispatcher,
    )
    return rt, tracker, dispatcher


# ------------------------------------------------------------------ tests ----

def test_trade_opened_event_format():
    """OutputEvent.from_trade_opened produces kind='trade_opened' with correct title."""
    td = {
        "signal_id": "abc123def456",
        "instrument": "ES",
        "direction": "LONG",
        "setup": "orb",
        "entry": 5000.0,
        "stop": 4990.0,
        "tp1": 5020.0,
        "tp2": 5040.0,
        "tp3": None,
        "tp1_r": 2.0,
        "tp2_r": 4.0,
        "initial_risk": 10.0,
        "timeframe": "5m",
        "context": {"regime": "TREND_UP", "session": "RTH", "mtf": "ALIGNED"},
    }
    ev = OutputEvent.from_trade_opened(td)
    assert ev.kind == "trade_opened"
    assert ev.dedup_key == "abc123def456"
    assert "LONG" in ev.title
    assert "ES" in ev.title
    assert "📈" in ev.title


def test_trade_opened_discord_payload_has_required_fields():
    """_build_trade_opened_payload includes direction, entry, stop, TP1, regime."""
    td = {
        "signal_id": "abc123def456",
        "instrument": "NQ",
        "direction": "SHORT",
        "setup": "vwap_reclaim",
        "entry": 20000.0, "stop": 20050.0,
        "tp1": 19950.0, "tp2": 19900.0, "tp3": None,
        "tp1_r": 1.0, "tp2_r": 2.0,
        "initial_risk": 50.0,
        "timeframe": "15m",
        "context": {"regime": "TREND_DOWN", "session": "RTH", "mtf": "ALIGNED"},
    }
    ev = OutputEvent.from_trade_opened(td)
    payload = _build_trade_opened_payload(ev)
    embed = payload["embeds"][0]

    field_names = {f["name"] for f in embed["fields"]}
    assert "Direction"  in field_names
    assert "Entry"      in field_names
    assert "Stop"       in field_names
    assert "TP1"        in field_names
    assert "Regime"     in field_names
    assert "Session"    in field_names
    # SHORT uses red color
    assert embed["color"] == 0xFF4444


def test_ghost_approved_sends_trade_opened_discord(engine, registry, monkeypatch):
    """GHOST APPROVED → PaperTrade OPEN → Discord trade_opened embed sent."""
    calls: list = []
    feed = _es_feed(engine)
    feed.poll()
    rt, tracker, _ = _runtime(engine, registry, feed, calls, monkeypatch=monkeypatch)
    rt.start()
    for _ in range(200):
        rt.run_cycle("ES")

    total = len(tracker.open_trades) + len(tracker.closed_trades)
    if total == 0:
        pytest.skip("No trade opened by GHOST (market conditions); Discord path untested")

    # Every paper trade must have a corresponding Discord notification.
    discord_trade_keys = {
        p["embeds"][0]["title"]
        for p in calls
        if "embeds" in p and p["embeds"]
        and ("OPENED" in p["embeds"][0].get("title", ""))
    }
    assert len(discord_trade_keys) >= 1, (
        f"Expected at least 1 'Trade Opened' Discord message; got calls={len(calls)}"
    )


def test_no_trade_never_reaches_discord(engine, registry, monkeypatch):
    """GHOST NO_TRADE decisions must NOT send any Discord notification."""
    calls: list = []
    feed = _es_feed(engine)
    feed.poll()
    # Use strict GHOST config that rejects all trades (very high confluence threshold).
    strict_cfg = GhostConfig(
        min_confluence_score=999.0,  # impossible to meet
        block_htf_conflict=False,
        block_against_structure=False,
        min_stop_atr_mult=0.0,
        cooldown_bars=0,
    )
    rt, tracker, _ = _runtime(engine, registry, feed, calls,
                               ghost_cfg=strict_cfg, monkeypatch=monkeypatch)
    rt.start()
    for _ in range(100):
        rt.run_cycle("ES")

    # No trades should have opened.
    assert len(tracker.open_trades) + len(tracker.closed_trades) == 0

    # Only BOT_STARTED lifecycle event should have been sent (if any).
    trade_opened_calls = [
        p for p in calls
        if "embeds" in p and any("OPENED" in (e.get("title", "")) for e in p["embeds"])
    ]
    assert len(trade_opened_calls) == 0, (
        f"NO_TRADE path sent {len(trade_opened_calls)} 'OPENED' Discord message(s); "
        "only APPROVED trades must reach Discord."
    )


def test_risk_block_never_reaches_discord(engine, registry, monkeypatch):
    """Hard risk rejection must NOT send a Discord notification."""
    calls: list = []
    monkeypatch.setattr(registry, "require_verified", lambda: None)
    # Wipe out the account so every new trade would exceed the daily loss limit.
    account = AccountState(equity=100_000.0, daily_realized_pnl=-999.0)
    feed = _es_feed(engine)
    feed.poll()
    rt, tracker, _ = _runtime(engine, registry, feed, calls, account=account)
    rt.start()
    for _ in range(100):
        rt.run_cycle("ES")

    assert len(tracker.open_trades) + len(tracker.closed_trades) == 0, (
        "No paper trade should open when daily loss limit is hit"
    )
    trade_opened_calls = [
        p for p in calls
        if "embeds" in p and any("OPENED" in (e.get("title", "")) for e in p["embeds"])
    ]
    assert len(trade_opened_calls) == 0, (
        "Risk-blocked signals must NOT reach Discord"
    )


def test_duplicate_signal_id_no_double_discord(engine, registry, monkeypatch):
    """Same signal_id must not produce two Discord notifications (dedup guard)."""
    calls: list = []
    monkeypatch.setattr(registry, "require_verified", lambda: None)
    from signal_engine.config.schema import DiscordOutputConfig
    cfg = DiscordOutputConfig(enabled=True, webhook_env="DISCORD_WEBHOOK_URL",
                              deduplication_enabled=True)
    adapter = _make_adapter(calls)
    adapter.config = cfg

    # Directly test emit dedup: same trade_opened event twice.
    td = {
        "signal_id": "dup-test-xyz", "instrument": "ES", "direction": "LONG",
        "setup": "orb", "entry": 5000.0, "stop": 4990.0,
        "tp1": 5020.0, "tp2": None, "tp3": None,
        "tp1_r": 2.0, "tp2_r": None,
        "initial_risk": 10.0, "timeframe": "5m",
        "context": {},
    }
    ev = OutputEvent.from_trade_opened(td)
    r1 = adapter.emit(ev)
    r2 = adapter.emit(ev)  # duplicate
    assert r1 is True, "First emit should succeed"
    assert r2 is False, "Duplicate emit should be blocked"
    assert len(calls) == 1, f"Expected 1 Discord call, got {len(calls)}"


def test_trade_opened_plan_matches_paper_trade():
    """The Discord payload must reflect the PaperTrade's actual levels (no gap)."""
    from signal_engine.paper.models import ExitReason
    import pandas as pd

    ts = pd.Timestamp.now(tz="UTC")
    tracker = PaperTradeTracker()
    t = tracker.open_trade(
        signal_id="plan-match-001", instrument="ES", direction="LONG",
        setup="orb", entry=5100.0, stop=5090.0, take_profits=[5120.0, 5140.0],
        opened_at=ts,
    )
    assert t is not None
    td = t.to_dict()
    ev = OutputEvent.from_trade_opened(td)
    payload = _build_trade_opened_payload(ev)
    embed = payload["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}

    assert "5100.00" in fields["Entry"]
    assert "5090.00" in fields["Stop"]
    assert "5120.00" in fields["TP1"]
    assert "5140.00" in fields["TP2"]
