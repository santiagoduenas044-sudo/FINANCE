"""Run the Control Dashboard + bot loop (Phase 10-13).

Wires a LiveFeed per instrument into a BotRuntime, drives it with the
daemon-friendly BotService (polling, duplicate-candle prevention, reconnection,
heartbeat), exposes it over the FastAPI control/status API, and serves the
premium dashboard UI.

Data source (Phase 11): real delayed ES=F/NQ=F via Yahoo Finance, with an ES
simulated fallback (bundled sample data) when Yahoo is unreachable; NQ has no
fake fallback. See signal_engine/runtime/bootstrap.py.

Configuration (Phase 13) comes from the environment via ServerSettings:
    BOT_MODE=local|remote   BOT_HOST   BOT_PORT   BOT_SYMBOLS   BOT_CYCLE_SECONDS
    BOT_AUTO_START          DASHBOARD_TOKEN (remote auth)   BOT_CORS_ORIGINS
See .env.example and docs/PHASE13_RUNTIME_REMOTE.md.

    PYTHONPATH=. python scripts/run_dashboard.py
    # then open http://127.0.0.1:8000/

The bot starts OFFLINE by default (set BOT_AUTO_START=true to start ONLINE).
No broker, no order execution, no auto-trading. AI is not in the signal path.
"""
from __future__ import annotations

import socket
import sys

import uvicorn

from signal_engine.api.app import create_app
from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.output.discord_bot import start_command_bot
from signal_engine.runtime.bootstrap import build_runtime, configure_logging
from signal_engine.runtime.service import BotService


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((probe_host, port)) == 0


def main() -> None:
    settings = ServerSettings.from_env()
    settings.validate()  # refuse an unsafe public server (remote mode needs auth)
    logger = configure_logging(settings.log_file)
    logger.info("mode=%s host=%s port=%s auth=%s", settings.mode, settings.host,
                settings.port, "on" if settings.auth_enabled else "off")

    if _port_in_use(settings.host, settings.port):
        logger.error("port %s is already in use — a previous instance may be running.",
                     settings.port)
        raise SystemExit(1)

    runtime, feed_info = build_runtime(settings.symbols, logger=logger)
    if not runtime.live_feeds:
        logger.error("no data source available for any configured symbol.")
        raise SystemExit(1)

    service = BotService(runtime, cycle_seconds=settings.cycle_seconds,
                         auto_start=settings.auto_start, logger=logger)
    service.start_background()
    # Optional read-only !status bot; a no-op unless DISCORD_BOT_TOKEN is set.
    start_command_bot(runtime, logger=logger)
    # Make the boot state unambiguous in the (launchd/journald) logs.
    if settings.auto_start:
        logger.info("autostart ENABLED (BOT_AUTO_START) -> runtime is %s; "
                    "processing closed candles automatically.", runtime.status.value)
    else:
        logger.info("autostart DISABLED -> runtime is %s; press START in the "
                    "dashboard (or set BOT_AUTO_START=true) to begin.", runtime.status.value)

    app = create_app(runtime, settings=settings, service=service)

    # Stop the loop cleanly when uvicorn shuts down (Ctrl-C / SIGTERM).
    @app.on_event("shutdown")
    def _on_shutdown() -> None:
        service.request_stop()
        if runtime.status.value == "ONLINE":
            runtime.stop()

    logger.info("Dashboard: http://%s:%s/  (bot starts %s)",
                settings.host, settings.port,
                "ONLINE" if settings.auto_start else "OFFLINE — use Start button")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
