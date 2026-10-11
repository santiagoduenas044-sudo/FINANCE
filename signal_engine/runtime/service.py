"""BotService (Phase 13): the daemon-friendly loop that lets the bot run 24/7
independently of Claude Code / any IDE.

It wraps a BotRuntime (Phase 9) and drives it on a fixed interval:

    Yahoo delayed ES/NQ → poll → (Setup → Signal → Risk) → Discord + Dashboard

Responsibilities (all operational — NONE change trading logic):
  * Automatic polling at a sensible interval.
  * Duplicate-candle prevention — the pipeline runs at most once per closed
    execution candle per symbol, never re-processing the same bar every tick.
  * Automatic recovery: a tick never crashes the loop; feed reconnection /
    backoff is handled by LiveFeed (Phase 10), and per-cycle engine errors are
    caught by BotRuntime itself. The service keeps ticking through outages.
  * Heartbeat even when paused/offline, so the dashboard shows liveness.
  * Graceful startup/shutdown, including SIGINT/SIGTERM handling for systemd.
  * Unattended logging.

The trading engines (Data / Market Analysis / Setup / Signal / Risk) are only
orchestrated here, never modified. `run_cycle()` still enforces the ONLINE gate
and the no-lookahead / closed-bars contract exactly as before.
"""
from __future__ import annotations

import logging
import signal
import threading
from typing import Callable

import pandas as pd

from ..live.models import DataFeedStatus
from .engine import BotRuntime
from .models import BotStatus

_DEFAULT_LOGGER = logging.getLogger("signal_engine.service")


class BotService:
    def __init__(
        self,
        runtime: BotRuntime,
        cycle_seconds: float = 5.0,
        auto_start: bool = False,
        logger: logging.Logger | None = None,
        on_tick: Callable[["BotService"], None] | None = None,
        max_catchup_bars: int = 5,
    ):
        self.runtime = runtime
        self.cycle_seconds = max(0.5, float(cycle_seconds))
        self.auto_start = auto_start
        self.logger = logger or _DEFAULT_LOGGER
        self.on_tick = on_tick
        # A delayed feed can deliver several closed candles in one poll. Each
        # is run in order (up to this many) so a stop or zone touch on a
        # middle candle is not skipped. Older gaps are not replayed.
        self.max_catchup_bars = max(1, int(max_catchup_bars))

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Last CLOSED execution-bar timestamp we actually processed, per symbol.
        # This is the duplicate-candle guard: we never run the pipeline twice on
        # the same bar.
        self._last_processed_bar: dict[str, pd.Timestamp] = {}

        # Lightweight, JSON-safe operational counters (for logs / health).
        self.tick_count = 0
        self.cycles_run = 0
        self.duplicates_skipped = 0
        self.errors = 0
        self.started = False

    # ------------------------------------------------------------------ #
    # One iteration — must never raise
    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        self.tick_count += 1
        try:
            if self.runtime.status is BotStatus.ONLINE:
                for symbol in list((self.runtime.live_feeds or {}).keys()):
                    self._process_symbol(symbol)
            else:
                # Not trading: still poll feeds so the dashboard chart stays
                # live, and heartbeat so liveness is visible. No pipeline runs.
                for symbol in list((self.runtime.live_feeds or {}).keys()):
                    self._refresh_feed(symbol)
                self.runtime.heartbeat()
            # Runs in both states on purpose: a digest saying the bot is PAUSED
            # is exactly the one the operator needs while away from home.
            self.runtime.maybe_send_status_digest()
            self.runtime.maybe_send_daily_notes()
            self.runtime.maybe_send_briefing()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            self.errors += 1
            self.logger.exception("unexpected error in service tick (loop continues)")
        finally:
            if self.on_tick is not None:
                try:
                    self.on_tick(self)
                except Exception:  # noqa: BLE001
                    self.logger.exception("on_tick callback failed (ignored)")

    def _refresh_feed(self, symbol: str) -> None:
        """Poll a feed for data + reflect health, without running the pipeline.
        Reuses the runtime's own poll/health logic (single source of truth)."""
        try:
            self.runtime._pull_live_bars(symbol)
        except Exception:  # noqa: BLE001 - never let a feed hiccup break the loop
            self.errors += 1
            self.logger.exception("feed refresh failed for %s (loop continues)", symbol)

    def _process_symbol(self, symbol: str) -> None:
        # Single poll + health via the runtime's own helper (no double polling).
        try:
            tf_bars = self.runtime._pull_live_bars(symbol)
        except Exception:  # noqa: BLE001
            self.errors += 1
            self.logger.exception("poll failed for %s (loop continues)", symbol)
            return
        if not tf_bars:
            return

        execution_tf = self.runtime.data_engine.cfg.timeframe_roles.execution
        bars = tf_bars.get(execution_tf)
        if bars is None or bars.empty:
            return

        last_ts = bars.index[-1]
        if self._last_processed_bar.get(symbol) == last_ts:
            # Same closed candle as last time — do not reprocess it.
            self.duplicates_skipped += 1
            return

        # Pass the already-polled bars so run_cycle does NOT poll again. We only
        # get here when a genuinely new closed candle exists, so the pipeline
        # runs at most once per candle per symbol.
        prev = self._last_processed_bar.get(symbol)
        pending = [last_ts] if prev is None else \
            list(bars.index[bars.index > prev][-self.max_catchup_bars:])
        for ts in pending:
            window = tf_bars if ts == last_ts else \
                {tf: df.loc[:ts] for tf, df in tf_bars.items()}
            result = self.runtime.run_cycle(symbol, tf_bars=window)
            if not result.ran:
                return
            self._last_processed_bar[symbol] = ts
            self.cycles_run += 1
            if result.signals_produced or result.no_trades_produced:
                self.logger.info(
                    "%s cycle @ %s: signals=%d no_trades=%d", symbol, ts,
                    result.signals_produced, result.no_trades_produced)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def run_forever(self, install_signal_handlers: bool = True) -> None:
        """Blocking loop for a headless daemon. Installs SIGINT/SIGTERM handlers
        (main thread only) so `systemctl stop` / Ctrl-C shut down gracefully."""
        if install_signal_handlers:
            self._install_signal_handlers()
        self.logger.info("BotService starting (cycle=%.1fs, symbols=%s, auto_start=%s)",
                         self.cycle_seconds, list((self.runtime.live_feeds or {}).keys()),
                         self.auto_start)
        self.started = True
        if self.auto_start and self.runtime.status is not BotStatus.ONLINE:
            self.runtime.start()
            self.logger.info("auto-start: runtime is ONLINE")
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.cycle_seconds)
        finally:
            self._graceful_shutdown()

    def start_background(self) -> threading.Thread:
        """Non-blocking loop in a daemon thread (used by the dashboard server so
        the same polling/dedup logic drives both). No signal handlers here —
        the web server owns process signals and calls `request_stop()`."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self.started = True
        if self.auto_start and self.runtime.status is not BotStatus.ONLINE:
            self.runtime.start()

        def _loop() -> None:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.cycle_seconds)

        self._thread = threading.Thread(target=_loop, name="bot-service", daemon=True)
        self._thread.start()
        return self._thread

    def request_stop(self) -> None:
        """Signal the loop to stop at the next opportunity (idempotent)."""
        self._stop.set()

    def _graceful_shutdown(self) -> None:
        self.logger.info("BotService shutting down gracefully...")
        try:
            if self.runtime.status is BotStatus.ONLINE:
                self.runtime.stop()  # emits BOT_STOPPED (+ Discord notice if configured)
        except Exception:  # noqa: BLE001
            self.logger.exception("error during graceful stop (continuing shutdown)")
        self.logger.info("BotService stopped. ticks=%d cycles=%d dup_skipped=%d errors=%d",
                         self.tick_count, self.cycles_run, self.duplicates_skipped, self.errors)

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.logger.info("received signal %s; requesting graceful stop", signum)
            self.request_stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not in main thread (e.g. under some test runners) — skip.
                self.logger.debug("could not install handler for signal %s", sig)

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        return {
            "running": self.started and not self._stop.is_set(),
            "cycle_seconds": self.cycle_seconds,
            "tick_count": self.tick_count,
            "cycles_run": self.cycles_run,
            "duplicates_skipped": self.duplicates_skipped,
            "errors": self.errors,
            "last_processed_bar": {
                s: (ts.isoformat() if ts is not None else None)
                for s, ts in self._last_processed_bar.items()
            },
        }
