"""Run the Control Dashboard in LIVE mode (Phase 10, extended in Phase 11).

Wires a LiveFeed per instrument into a BotRuntime, exposes it over the
FastAPI control/status API, and serves the dashboard UI. The bot starts
OFFLINE — use the dashboard's Start button (or POST /api/control/start) to
actually turn it on; a background loop then calls `run_cycle()` every few
seconds for each configured symbol, but ONLY does anything while ONLINE.

Data source (Phase 11): tries YahooDelayedAdapter first for each symbol (real,
delayed ES=F/NQ=F quotes, no API key needed — see signal_engine/live/
yahoo_adapter.py for exactly how delayed and why this provider). If it can't
connect (e.g. no network reachability, or a sandbox with restricted egress),
ES falls back to SimulatedLiveAdapter (replaying the bundled ES sample data)
exactly as in Phase 10; NQ has no bundled sample data, so it is simply
skipped when the real source is unreachable rather than fabricating one.

Swapping the data source further: implement `LiveDataAdapter` for any other
vendor (Databento, Interactive Brokers, ...) — nothing else in this script,
or anywhere upstream (LiveFeed, BotRuntime, Setup/Signal/Risk Engine), needs
to change. See signal_engine/live/adapter.py.

Usage:
    PYTHONPATH=. python scripts/run_dashboard.py
    # then open http://127.0.0.1:8000/

Discord notifications (bot lifecycle + real signals) are sent only if
output.discord.enabled=true in config AND DISCORD_WEBHOOK_URL is set in the
environment (see .env.example) — otherwise they are safely skipped.

Every Signal/NoTrade is also appended to data_store/signal_log.jsonl (JSON
Lines, one record per line) so the dashboard's history survives a restart —
see GET /api/signals/history.
"""
from __future__ import annotations

import socket
import sys
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
from signal_engine.live.yahoo_adapter import YahooDelayedAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"
SIGNAL_LOG_PATH = ROOT / "data_store" / "signal_log.jsonl"
SYMBOLS = ["ES", "NQ"]
CYCLE_SECONDS = 5
BARS_PER_TICK = 5   # how many 1m bars the simulated feed "delivers" per cycle
HOST = "127.0.0.1"
PORT = 8000

# Symbol -> (sample CSV directory name, contract) for the SimulatedLiveAdapter
# fallback. Only symbols listed here can fall back when the real source is
# unreachable — NQ has no bundled sample data, so it is intentionally absent.
_SIMULATED_FALLBACK_SOURCE = {"ES": "ESM26"}


def _build_live_feed(symbol: str, data_engine: DataEngine) -> LiveFeed | None:
    real_adapter = YahooDelayedAdapter(timezone=data_engine.cfg.data.timezone)
    try:
        real_adapter.connect()
        print(f"[{symbol}] connected to Yahoo Finance (delayed) live data.")
        return LiveFeed(real_adapter, symbol, data_engine.cfg)
    except ConnectionError as exc:
        print(f"[{symbol}] Yahoo Finance unreachable ({type(exc).__name__}); "
              f"falling back...")

    contract = _SIMULATED_FALLBACK_SOURCE.get(symbol)
    if contract is None:
        print(f"[{symbol}] no simulated fallback data available — skipping this symbol.")
        return None

    provider = CsvProvider(SAMPLES)
    raw = provider.get_raw_bars(symbol, "1m", contract=contract)
    historical_1m = normalize(raw, data_engine.cfg.data, "1m")
    sim_adapter = SimulatedLiveAdapter(historical_1m, symbol=symbol, bars_per_poll=BARS_PER_TICK)
    print(f"[{symbol}] using SimulatedLiveAdapter (replaying bundled sample data).")
    return LiveFeed(sim_adapter, symbol, data_engine.cfg)


def build_runtime() -> BotRuntime:
    data_engine = DataEngine()
    registry = ContractRegistry.load()

    live_feeds = {}
    for symbol in SYMBOLS:
        feed = _build_live_feed(symbol, data_engine)
        if feed is not None:
            live_feeds[symbol] = feed

    setup_engine = SetupEngine(data_engine.cfg, registry)
    signal_engine = SignalEngine(data_engine.cfg.signals)
    dispatcher = OutputDispatcher.from_config(data_engine.cfg)   # Discord if enabled+configured

    return BotRuntime(data_engine, setup_engine, signal_engine, registry,
                      output_dispatcher=dispatcher, live_feeds=live_feeds,
                      signal_log_path=SIGNAL_LOG_PATH)


def background_loop(runtime: BotRuntime) -> None:
    while True:
        if runtime.status.value == "ONLINE":
            # run_cycle() polls each feed itself and then runs the full
            # (unchanged) Setup -> Signal -> Risk pipeline.
            for symbol in (runtime.live_feeds or {}):
                runtime.run_cycle(symbol)     # no tf_bars -> pulls from the live feed
        else:
            # Bot is OFFLINE/PAUSED/ERROR: never run the trading pipeline, but
            # DO keep pulling delayed market data so the dashboard's live chart
            # stays real (this only fetches bars into the feed buffer — it does
            # not detect setups, generate signals, size risk, or place orders).
            for feed in (runtime.live_feeds or {}).values():
                try:
                    feed.poll()
                except Exception:  # noqa: BLE001 - data refresh must never crash the loop
                    pass
            runtime.heartbeat()
        time.sleep(CYCLE_SECONDS)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def main() -> None:
    if _port_in_use(HOST, PORT):
        print(
            f"ERROR: port {PORT} on {HOST} is already in use — a previous "
            f"dashboard instance is likely still running.\n"
            f"Find and stop it first, e.g.:\n"
            f"  lsof -i :{PORT}          # or: ss -ltnp | grep {PORT}\n"
            f"  kill <PID>               # then re-run this script",
            file=sys.stderr,
        )
        raise SystemExit(1)

    runtime = build_runtime()
    if not runtime.live_feeds:
        print("ERROR: no data source available for any configured symbol.", file=sys.stderr)
        raise SystemExit(1)

    app = create_app(runtime)

    thread = threading.Thread(target=background_loop, args=(runtime,), daemon=True)
    thread.start()

    print(f"Dashboard: http://{HOST}:{PORT}/  (bot starts OFFLINE — use Start button)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
