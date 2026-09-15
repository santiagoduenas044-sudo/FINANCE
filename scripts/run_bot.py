"""Headless 24/7 bot runner (Phase 13).

Runs the trading pipeline WITHOUT the dashboard web server — the smallest
possible always-on process for a VPS/systemd/Docker deployment:

    Yahoo delayed ES/NQ → BotService → (Setup → Signal → Risk) → Discord + signal log

It shares the exact same wiring as the dashboard (signal_engine.runtime.
bootstrap), so both behave identically; the only difference is that this one
serves no HTTP and starts trading automatically (BOT_AUTO_START defaults to
true here). Signals are persisted to data_store/signal_log.jsonl, so a
dashboard started later (even elsewhere) can show the history.

Graceful shutdown: SIGINT/SIGTERM stop the loop cleanly (BotService installs the
handlers), so `systemctl stop` / `docker stop` are clean.

    PYTHONPATH=. BOT_AUTO_START=true python scripts/run_bot.py

No broker, no order execution, no auto-trading of a broker account, no AI in the
signal path — this only DETECTS and PUBLISHES signals.
"""
from __future__ import annotations

import os

from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.runtime.bootstrap import build_runtime, configure_logging
from signal_engine.runtime.service import BotService


def main() -> None:
    settings = ServerSettings.from_env()
    # Headless runner trades by default (that's the whole point of a 24/7 bot),
    # unless the operator explicitly set BOT_AUTO_START.
    auto_start = settings.auto_start if os.environ.get("BOT_AUTO_START") else True

    logger = configure_logging(settings.log_file)
    logger.info("headless bot runner starting: symbols=%s cycle=%.1fs auto_start=%s",
                settings.symbols, settings.cycle_seconds, auto_start)

    runtime, feed_info = build_runtime(settings.symbols, logger=logger)
    if not runtime.live_feeds:
        logger.error("no data source available for any configured symbol — exiting.")
        raise SystemExit(1)
    for sym, info in feed_info.items():
        logger.info("feed[%s]: source=%s delayed=%s simulated=%s",
                    sym, info.source, info.delayed, info.simulated)

    service = BotService(runtime, cycle_seconds=settings.cycle_seconds,
                         auto_start=auto_start, logger=logger)
    # Blocking loop with SIGINT/SIGTERM handling → clean systemd/docker stop.
    service.run_forever()


if __name__ == "__main__":
    main()
