"""Run the Control Dashboard (Phase 9).

Wires DataEngine + SetupEngine + SignalEngine (+ optional RiskEngine) into a
BotRuntime, exposes it over the FastAPI control/status API, and serves the
dashboard UI. The bot starts OFFLINE — use the dashboard's Start button (or
POST /api/control/start) to actually turn it on; a background loop then
calls `run_cycle()` every few seconds, but ONLY does anything while ONLINE.

Usage:
    PYTHONPATH=. python scripts/run_dashboard.py
    # then open http://127.0.0.1:8000/

Discord notifications on start/pause/stop are sent only if
output.discord.enabled=true in config AND DISCORD_WEBHOOK_URL is set in the
environment (see .env.example) — otherwise they are safely skipped.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn

from signal_engine.api.app import create_app
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"
CYCLE_SECONDS = 5


def build_runtime() -> tuple[BotRuntime, DataEngine, dict]:
    data_engine = DataEngine()
    registry = ContractRegistry.load()
    provider = CsvProvider(SAMPLES)
    lc = data_engine.load_contract(provider, "ES", "ESM26", "1m")
    data_engine.build_timeframes(lc)

    setup_engine = SetupEngine(data_engine.cfg, registry)
    signal_engine = SignalEngine(data_engine.cfg.signals)
    dispatcher = OutputDispatcher.from_config(data_engine.cfg)   # Discord if enabled+configured

    runtime = BotRuntime(data_engine, setup_engine, signal_engine, registry,
                         output_dispatcher=dispatcher)
    return runtime, data_engine, lc.timeframes


def background_loop(runtime: BotRuntime, tf_bars: dict) -> None:
    while True:
        if runtime.status.value == "ONLINE":
            runtime.run_cycle("ES", tf_bars)
        else:
            runtime.heartbeat()
        time.sleep(CYCLE_SECONDS)


def main() -> None:
    runtime, _, tf_bars = build_runtime()
    app = create_app(runtime)

    thread = threading.Thread(target=background_loop, args=(runtime, tf_bars), daemon=True)
    thread.start()

    print("Dashboard: http://127.0.0.1:8000/  (bot starts OFFLINE — use Start button)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
