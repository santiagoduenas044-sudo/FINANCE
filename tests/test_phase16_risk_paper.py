"""Phase 16 — Risk engine wiring + paper trading integration.

Covers the Phase 16 acceptance criteria:
  1. risk_engine is wired by build_runtime (no more "DISABLED" on dashboard).
  2. UNVERIFIED_CONTRACT_SPEC → soft bypass: paper trade opens anyway.
  3. Hard risk rejection (daily loss limit) → paper trade blocked.
  4. AccountState.trades_today survives restart via paper_tracker persistence.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig, GhostConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.ghost.brain import GhostBrain
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.risk.engine import RiskEngine
from signal_engine.risk.models import AccountState
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

SAMPLES = Path(__file__).resolve().parent / "data_samples"


# ------------------------------------------------------------------ helpers ---

def _es_feed(engine, bars_per_poll=5):
    hist = normalize(CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26"),
                     engine.cfg.data, "1m")
    return LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=bars_per_poll),
                    "ES", engine.cfg)


def _runtime_with_risk(engine, registry, feed, account=None, tmp_paper=None,
                       ghost_cfg=None):
    """Build a BotRuntime fully wired with risk engine and optional account."""
    gcfg = ghost_cfg or GhostConfig(min_confluence_score=0.0, block_htf_conflict=False,
                                     block_against_structure=False, min_stop_atr_mult=0.0,
                                     cooldown_bars=0)
    ghost = GhostBrain(gcfg)
    tracker = PaperTradeTracker(store_dir=tmp_paper) if tmp_paper is not None else PaperTradeTracker()
    risk = RiskEngine(engine.cfg.risk, registry)
    acct = account or AccountState(equity=100_000.0)
    return BotRuntime(
        engine, SetupEngine(engine.cfg, registry),
        SignalEngine(engine.cfg.signals), registry,
        risk_engine=risk, account=acct,
        live_feeds={"ES": feed},
        ghost=ghost, paper_tracker=tracker,
    ), tracker


# ------------------------------------------------------------------ tests ----

def test_risk_engine_wired_in_bootstrap(monkeypatch):
    """build_runtime must wire a RiskEngine — no more DISABLED on the dashboard."""
    # Force Yahoo to fail immediately so the test doesn't wait for a network call.
    def _no_connect(self):
        raise ConnectionError("test: no network")
    monkeypatch.setattr(
        "signal_engine.live.yahoo_adapter.YahooDelayedAdapter.connect",
        _no_connect,
    )
    # Also disable ghost + paper so bootstrap doesn't touch DEFAULT_PAPER_DIR.
    from signal_engine.config.schema import GhostConfig
    cfg = EngineConfig.load()
    cfg.ghost.enabled = False

    from signal_engine.runtime.bootstrap import build_runtime
    runtime, _ = build_runtime(["ES"], config=cfg, signal_log_path=None)
    assert runtime.risk_engine is not None, (
        "RiskEngine must be wired by build_runtime; got None — check bootstrap.py"
    )


def test_unverified_spec_opens_paper_trade_anyway(engine, registry):
    """GHOST-approved signal opens a paper trade despite CME-unverified rejection.

    require_verified() always fails (no CME data in tests), so the risk engine
    returns UNVERIFIED_CONTRACT_SPEC. The soft bypass in _run_ghost must let the
    paper trade open regardless.
    """
    feed = _es_feed(engine)
    feed.poll()
    rt, tracker = _runtime_with_risk(engine, registry, feed)
    rt.start()
    for _ in range(200):
        rt.run_cycle("ES")
    total = len(tracker.open_trades) + len(tracker.closed_trades)
    # With permissive GHOST config, at least one trade should open despite
    # UNVERIFIED_CONTRACT_SPEC (soft bypass active).
    assert total >= 1, (
        "Expected at least 1 paper trade even with unverified specs — "
        "UNVERIFIED_CONTRACT_SPEC must be treated as a soft bypass, not a hard block."
    )


def test_risk_engine_blocks_on_daily_loss_limit(engine, registry, monkeypatch):
    """Daily loss limit blocks paper trades even when GHOST approves.

    We patch require_verified() so the risk engine reaches the daily-limit check
    (otherwise UNVERIFIED_CONTRACT_SPEC fires first and soft-bypasses).
    """
    # Patch require_verified so the risk engine reaches the daily P&L checks.
    monkeypatch.setattr(registry, "require_verified", lambda: None)

    # -999 realized P&L with default fixed_risk_dollar=500 and
    # daily_loss_limit_dollar=1000 triggers DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED
    # because -999 - 500 = -1499 <= -1000.
    account = AccountState(equity=100_000.0, daily_realized_pnl=-999.0)
    feed = _es_feed(engine)
    feed.poll()
    rt, tracker = _runtime_with_risk(engine, registry, feed, account=account)
    rt.start()
    for _ in range(200):
        rt.run_cycle("ES")
    total = len(tracker.open_trades) + len(tracker.closed_trades)
    assert total == 0, (
        f"Expected 0 paper trades when daily loss limit is hit; got {total}. "
        "Risk engine must hard-block non-UNVERIFIED rejections."
    )


def test_account_state_survives_restart(engine, registry, tmp_path, monkeypatch):
    """AccountState.trades_today reflects today's closed trades after a restart.

    Simulates a previous session that opened and closed a paper trade, then
    calls build_runtime (the restart path) and asserts that account.trades_today
    is >= 1 (loaded from the persisted closed trades).
    """
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()

    # Simulate a previous session: open and close a trade (today).
    tracker = PaperTradeTracker(store_dir=paper_dir)
    ts = pd.Timestamp.now(tz="America/New_York")
    t = tracker.open_trade(
        signal_id="test-restart-123", instrument="ES", direction="LONG",
        setup="orb", entry=5000.0, stop=4990.0, take_profits=[5020.0],
        opened_at=ts,
    )
    assert t is not None, "open_trade must succeed"
    # Feed a bar that hits TP1 to close the trade
    bar = pd.Series({"open": 5000, "high": 5025, "low": 4995, "close": 5020})
    tracker.update_on_bar(t, bar, ts + pd.Timedelta(minutes=5))
    assert len(tracker.closed_trades) == 1, "trade must have closed on TP1"

    # Simulate restart: patch DEFAULT_PAPER_DIR to the tmp paper dir.
    monkeypatch.setattr("signal_engine.runtime.bootstrap.DEFAULT_PAPER_DIR", paper_dir)
    # Force Yahoo to fail so the test is fast.
    def _no_connect(self):
        raise ConnectionError("test: no network")
    monkeypatch.setattr(
        "signal_engine.live.yahoo_adapter.YahooDelayedAdapter.connect",
        _no_connect,
    )

    # Disable ghost to keep the build simple and focus on AccountState loading.
    cfg = EngineConfig.load()
    cfg.ghost.enabled = True
    cfg.ghost.paper_enabled = True

    from signal_engine.runtime.bootstrap import build_runtime
    runtime, _ = build_runtime(["ES"], config=cfg, signal_log_path=None)

    # The restarted runtime must have loaded today's closed trade count.
    assert runtime.account is not None, "account must be set after restart"
    assert runtime.account.trades_today >= 1, (
        f"Expected trades_today >= 1 after restart (found {runtime.account.trades_today}). "
        "Fix 2 in bootstrap.py must load today's closed trades from paper_tracker."
    )
