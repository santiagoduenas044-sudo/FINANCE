"""Discord output adapter demo (Phase 5.5).

Wires SetupEngine -> OutputEvent -> DiscordOutputAdapter -> Discord, WITHOUT
touching the Setup Engine core (it stays Discord-agnostic). By default this
does NOT send anything unless DISCORD_WEBHOOK_URL is set in the environment
AND output.discord.enabled is turned on in config.

Usage:
    cp .env.example .env   # then edit .env and set DISCORD_WEBHOOK_URL
    export $(cat .env | xargs)   # or use a tool like python-dotenv
    PYTHONPATH=. python scripts/demo_discord_output.py
"""
from __future__ import annotations

from pathlib import Path

from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.setups.engine import SetupEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def main() -> None:
    data_engine = DataEngine()
    reg = ContractRegistry.load()
    provider = CsvProvider(SAMPLES)
    lc = data_engine.load_contract(provider, "ES", "ESM26", "1m")
    data_engine.build_timeframes(lc)

    # Explicitly enable Discord output for this demo run (core config default
    # is `enabled: false`, so this is opt-in and does not affect the core).
    data_engine.cfg.output.discord.enabled = True

    setup_engine = SetupEngine(data_engine.cfg, reg)
    dispatcher = OutputDispatcher.from_config(data_engine.cfg)

    if not dispatcher.adapters:
        print("Discord adapter not built (should not happen since enabled=True above).")
        return
    discord = dispatcher.adapters[0]
    print(f"Discord adapter: {discord!r}")   # __repr__ never leaks the webhook
    if not discord.configured:
        print("DISCORD_WEBHOOK_URL is not set — candidates will be detected "
              "but emit() will be skipped (no message sent). This is expected "
              "and safe for a dry run.")

    dts = list(lc.timeframes["5m"].index[10:])
    results = setup_engine.scan(data_engine, "ES", lc.timeframes, dts)

    total = sum(len(cs) for cs in results.values())
    print(f"Detected {total} setup candidate(s) across {len(results)} decision time(s).")

    sent = 0
    for _, candidates in results.items():
        for c in candidates:
            dispatcher.publish_candidate(c)
            sent += 1
            if sent >= 3:      # keep the demo small
                break
        if sent >= 3:
            break
    print(f"Dispatched {sent} candidate(s) through the output layer "
         f"(only actually posted to Discord if a real webhook is configured).")


if __name__ == "__main__":
    main()
