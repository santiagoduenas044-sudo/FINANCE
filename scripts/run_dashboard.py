"""Run the Control Dashboard in LIVE mode (Phase 10).

Wires a LiveFeed (SimulatedLiveAdapter by default — replays the ES sample
data as if it were arriving in real time) into a BotRuntime, exposes it over
the FastAPI control/status API, and serves the dashboard UI. The bot starts
OFFLINE — use the dashboard's Start button (or POST /api/control/start) to
actually turn it on; a background loop then calls `run_cycle()` every few
seconds, but ONLY does anything while ONLINE.

Swapping the data source: replace `SimulatedLiveAdapter` with any other
`LiveDataAdapter` implementation (Databento, Interactive Brokers, ...) —
nothing else in this script, or anywhere upstream (LiveFeed, BotRuntime,
Setup/Signal/Risk Engine), needs to change. See signal_engine/live/adapter.py.

Usage:
    PYTHONPATH=. python scripts/run_dashboard.py
    # then open http://127.0.0.1:8000/

Discord notifications (bot lifecycle + real signals) are sent only if
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
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"
SYMBOL = "ES"
CYCLE_SECONDS = 5
BARS_PER_TICK = 5   # how many 1m bars the simulated feed "delivers" per cycle


def build_runtime() -> BotRuntime:
    data_engine = DataEngine()
    registry = ContractRegistry.load()

    provider = CsvProvider(SAMPLES)
    raw = provider.get_raw_bars(SYMBOL, "1m", contract="ESM26")
    historical_1m = normalize(raw, data_engine.cfg.data, "1m")

    adapter = SimulatedLiveAdapter(historical_1m, symbol=SYMBOL, bars_per_poll=BARS_PER_TICK)
    live_feed = LiveFeed(adapter, SYMBOL, data_engine.cfg)

    setup_engine = SetupEngine(data_engine.cfg, registry)
    signal_engine = SignalEngine(data_engine.cfg.signals)
    dispatcher = OutputDispatcher.from_config(data_engine.cfg)   # Discord if enabled+configured

    return BotRuntime(data_engine, setup_engine, signal_engine, registry,
                      output_dispatcher=dispatcher, live_feeds={SYMBOL: live_feed})


def background_loop(runtime: BotRuntime) -> None:
    while True:
        if runtime.status.value == "ONLINE":
            runtime.run_cycle(SYMBOL)     # no tf_bars -> pulls from the live feed
        else:
            runtime.heartbeat()
        time.sleep(CYCLE_SECONDS)


def main() -> None:
    runtime = build_runtime()
    app = create_app(runtime)

    thread = threading.Thread(target=background_loop, args=(runtime,), daemon=True)
    thread.start()

    print("LIVE mode (simulated feed replaying ES sample data).")
    print("Dashboard: http://127.0.0.1:8000/  (bot starts OFFLINE — use Start button)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
