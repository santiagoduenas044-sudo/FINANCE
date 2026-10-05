"""Runtime bootstrap (Phase 13): build a fully-wired BotRuntime the same way
for every entry point — the dashboard server, the headless 24/7 runner, and
tests. Previously this lived inside scripts/run_dashboard.py; centralizing it
keeps the data-source wiring (real Yahoo delayed → simulated fallback) in ONE
place so both the dashboard and the daemon behave identically.

Nothing here changes the trading engines. It only *wires them together* exactly
as the earlier phases already did:

    Yahoo delayed ES/NQ → LiveFeed → BotRuntime → (Setup → Signal → Risk) → Discord + Dashboard

Data source policy (unchanged from Phase 11):
  * Try YahooDelayedAdapter first for each symbol (real delayed ES=F/NQ=F).
  * If unreachable, ES falls back to replaying the bundled ES sample data
    (SimulatedLiveAdapter); NQ has no bundled sample, so it is skipped rather
    than fabricated.
The fallback is flagged (``simulated=True``) so the dashboard can show a strong,
honest visual distinction between real delayed data and the sample fallback.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

from ..config.schema import EngineConfig
from ..data.contracts import ContractRegistry
from ..data.engine import DataEngine
from ..data.ingestion import normalize
from ..data.providers.csv_provider import CsvProvider
from ..live.feed import LiveFeed
from ..live.simulated import SimulatedLiveAdapter
from ..live.yahoo_adapter import YahooDelayedAdapter
from ..output.dispatcher import OutputDispatcher
from ..setups.engine import SetupEngine
from ..signals.engine import SignalEngine
from .engine import BotRuntime

ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLES = ROOT / "tests" / "data_samples"
DEFAULT_SIGNAL_LOG = ROOT / "data_store" / "signal_log.jsonl"
DEFAULT_GHOST_LOG = ROOT / "data_store" / "ghost_log.jsonl"
DEFAULT_PAPER_DIR = ROOT / "data_store" / "paper"
DEFAULT_IDEAS_FILE = ROOT / "data_store" / "ideas.json"
BARS_PER_TICK = 5  # how many 1m bars the simulated fallback delivers per poll

# Symbol -> bundled sample contract for the SimulatedLiveAdapter fallback.
# Only symbols listed here can fall back when the real source is unreachable —
# NQ is intentionally absent (no fabricated data).
_SIMULATED_FALLBACK_SOURCE = {"ES": "ESM26"}


def configure_logging(log_file: str = "", level: int = logging.INFO) -> logging.Logger:
    """Set up logging suitable for unattended operation: timestamped lines to
    stdout (captured by systemd/journald or Docker) plus an optional rotating
    file. Returns the ``signal_engine`` logger. Idempotent-ish: clears prior
    handlers so re-running in one process doesn't duplicate lines."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("signal_engine")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    root.propagate = False
    return root


@dataclass(frozen=True)
class FeedInfo:
    """What actually got wired for a symbol — surfaced so the dashboard/logs
    can distinguish real delayed data from the sample fallback."""
    symbol: str
    source: str            # adapter name, e.g. "yahoo_delayed" / "simulated"
    delayed: bool          # real-but-delayed source?
    simulated: bool        # replaying bundled sample data (NOT real market data)?


def build_live_feed(symbol: str, data_engine: DataEngine, *, logger=None,
                    allow_simulated_fallback: bool = True):
    """Return (LiveFeed | None, FeedInfo | None) for `symbol`."""
    def _log(msg: str) -> None:
        (logger.info if logger else print)(msg)

    real_adapter = YahooDelayedAdapter(timezone=data_engine.cfg.data.timezone)
    try:
        real_adapter.connect()
        _log(f"[{symbol}] connected to Yahoo Finance (delayed) live data.")
        feed = LiveFeed(real_adapter, symbol, data_engine.cfg)
        return feed, FeedInfo(symbol, real_adapter.name, True, False)
    except ConnectionError as exc:
        _log(f"[{symbol}] Yahoo Finance unreachable ({type(exc).__name__}); falling back...")

    contract = _SIMULATED_FALLBACK_SOURCE.get(symbol) if allow_simulated_fallback else None
    if contract is None:
        _log(f"[{symbol}] no simulated fallback data available — skipping this symbol.")
        return None, None

    provider = CsvProvider(SAMPLES)
    raw = provider.get_raw_bars(symbol, "1m", contract=contract)
    historical_1m = normalize(raw, data_engine.cfg.data, "1m")
    sim_adapter = SimulatedLiveAdapter(historical_1m, symbol=symbol, bars_per_poll=BARS_PER_TICK)
    _log(f"[{symbol}] using SimulatedLiveAdapter (replaying bundled sample data).")
    feed = LiveFeed(sim_adapter, symbol, data_engine.cfg)
    return feed, FeedInfo(symbol, sim_adapter.name, False, True)


def build_runtime(
    symbols: list[str],
    *,
    config: EngineConfig | None = None,
    signal_log_path: str | Path | None = DEFAULT_SIGNAL_LOG,
    logger=None,
    allow_simulated_fallback: bool = True,
    data_dir: Path | None = None,
) -> tuple[BotRuntime, dict[str, FeedInfo]]:
    """Wire a BotRuntime for `symbols`. Returns (runtime, feed_info_by_symbol).
    `data_dir`: keep the paper trades, GHOST log and ideas there instead of
    data_store/ (replays must never touch the real record)."""
    paper_dir, ghost_log_file, ideas_file = (
        (DEFAULT_PAPER_DIR, DEFAULT_GHOST_LOG, DEFAULT_IDEAS_FILE) if data_dir is None
        else (Path(data_dir) / "paper", Path(data_dir) / "ghost_log.jsonl",
              Path(data_dir) / "ideas.json"))
    # No explicit config -> what the live bot runs: default.yaml plus the
    # strategy overlay named by $BOT_STRATEGY (default "zones").
    data_engine = DataEngine(config if config is not None else EngineConfig.load_active())
    registry = ContractRegistry.load()

    live_feeds: dict[str, LiveFeed] = {}
    feed_info: dict[str, FeedInfo] = {}
    for symbol in symbols:
        feed, info = build_live_feed(symbol, data_engine, logger=logger,
                                     allow_simulated_fallback=allow_simulated_fallback)
        if feed is not None and info is not None:
            live_feeds[symbol] = feed
            feed_info[symbol] = info

    setup_engine = SetupEngine(data_engine.cfg, registry)
    signal_engine = SignalEngine(data_engine.cfg.signals)
    dispatcher = OutputDispatcher.from_config(data_engine.cfg)  # Discord if enabled+configured

    # Phase 14: GHOST brain + paper-trade memory (additive; disabled -> identical
    # behaviour to before). No new connections, no orders, no AI in the path.
    ghost = paper_tracker = ghost_log = None
    gcfg = data_engine.cfg.ghost
    if gcfg.enabled:
        from ..ghost.brain import GhostBrain
        from ..paper.tracker import PaperTradeTracker
        ghost = GhostBrain(gcfg)
        ghost_log = ghost_log_file
        if gcfg.paper_enabled:
            paper_tracker = PaperTradeTracker.from_config(gcfg, store_dir=paper_dir)

    # Phase 16 Fix 1: Wire RiskEngine so the dashboard shows a real risk status
    # instead of "DISABLED". The CME require_verified() guard remains intact —
    # see signal_engine/runtime/engine.py _run_ghost() for the soft bypass when
    # specs are unverified (paper trade still opens, sized with zero dollars).
    from ..risk.engine import RiskEngine
    from ..risk.models import AccountState
    risk_engine = RiskEngine(data_engine.cfg.risk, registry)

    # Phase 16 Fix 2: Seed daily P&L and trade count from paper_tracker's
    # already-closed trades so account state survives a restart.
    # Dates are compared in the trading timezone (not UTC) to avoid a
    # mismatch when the server clock crosses midnight UTC while the trading
    # calendar is still on the previous day in New York (UTC-4/5).
    if paper_tracker is not None:
        import pandas as _pd
        _tz = data_engine.cfg.data.timezone   # "America/New_York"
        _today = _pd.Timestamp.now(tz=_tz).date()
        _today_closed = []
        for _ct in paper_tracker.closed_trades:
            if _ct.closed_at is None:
                continue
            try:
                _ca = _pd.Timestamp(_ct.closed_at)
                _ca = _ca.tz_localize(_tz) if _ca.tzinfo is None else _ca.tz_convert(_tz)
                if _ca.date() == _today:
                    _today_closed.append(_ct)
            except Exception:  # noqa: BLE001
                pass
        account = AccountState(
            equity=100_000.0,
            trades_today=len(_today_closed),
            daily_realized_pnl=float(sum(t.pnl_points for t in _today_closed)),
        )
    else:
        account = AccountState(equity=100_000.0)

    runtime = BotRuntime(
        data_engine, setup_engine, signal_engine, registry,
        risk_engine=risk_engine, account=account,
        output_dispatcher=dispatcher, live_feeds=live_feeds,
        signal_log_path=signal_log_path,
        ghost=ghost, paper_tracker=paper_tracker, ghost_log_path=ghost_log,
    )
    # economic calendar for the live bot (BOT_NEWS=off disables); replays do without
    if ghost is not None and data_dir is None and \
            os.environ.get("BOT_NEWS", "on").strip().lower() not in ("off", "0", "false"):
        from ..news import NewsCalendar
        runtime.news = NewsCalendar()
    # the morning Bible verse and midday curious fact (BOT_VERSICULOS / BOT_DATOS)
    if data_dir is None:
        from ..output.inspiration import DailyNotes
        runtime.daily_notes = DailyNotes(DEFAULT_PAPER_DIR.parent / "inspiration.json")
    # parallel paper variants (config/shadows.yaml); BOT_SHADOWS=off disables
    if ghost is not None and data_dir is None and \
            os.environ.get("BOT_SHADOWS", "on").strip().lower() not in ("off", "0", "false"):
        from .shadows import build_ghost_shadows
        runtime.shadows = build_ghost_shadows(registry, risk_engine=risk_engine)
    zr = data_engine.cfg.setups.zone_retrace
    if zr.enabled and ghost is not None:
        from .ideas import IdeaBoard
        runtime.idea_board = IdeaBoard(ideas_file,
                                       timezone=data_engine.cfg.data.timezone)

    # Record each feed's real source on the runtime so status/chart can report
    # simulated-vs-delayed honestly (read-only metadata; no logic depends on it).
    runtime.feed_info = {s: {"source": i.source, "delayed": i.delayed,
                             "simulated": i.simulated}
                         for s, i in feed_info.items()}
    return runtime, feed_info
