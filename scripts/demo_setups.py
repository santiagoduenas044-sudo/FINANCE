"""Setup Engine demo (Phase 5). Scans the sample data with all three setups
enabled and prints example SetupCandidates. Detection only — no signal/scoring/
risk/sizing.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.setups.engine import SetupEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def main():
    engine = DataEngine()
    reg = ContractRegistry.load()
    provider = CsvProvider(SAMPLES)
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    engine.build_timeframes(lc)

    se = SetupEngine(engine.cfg, reg)
    print("enabled setups:", se.enabled_setups)
    print("structure backend:", engine.cfg.analysis.structure_backend)

    dts = list(lc.timeframes["5m"].index[10:])
    results = se.scan(engine, "ES", lc.timeframes, dts)

    from collections import Counter
    cnt = Counter(c.setup_name for cs in results.values() for c in cs)
    print("\ncandidates by setup:", dict(cnt))

    shown = {"liquidity_sweep": 0, "vwap_reclaim": 0, "orb": 0}
    print("\n--- example candidates (one per setup) ---")
    for N, cs in results.items():
        for c in cs:
            if shown[c.setup_name] == 0:
                shown[c.setup_name] = 1
                print(f"\n[{c.setup_name}] {c.instrument} {c.direction.value} @ {c.timestamp:%m-%d %H:%M} ({c.timeframe})")
                print(f"  entry={c.entry:.2f} stop={c.stop_loss:.2f} "
                      f"tp={[round(t, 2) for t in c.take_profits]} rr={[round(r, 2) for r in c.risk_reward]}")
                print(f"  status={c.status.value} valid_until={c.valid_until:%m-%d %H:%M}")
                print(f"  reasons: {c.reasons}")
                print(f"  invalidation: {c.invalidation_conditions} (price={c.invalidation_price:.2f})")
                print(f"  referenced: {[(r.kind, round(r.price, 2)) for r in c.referenced]}")
                print(f"  context: {c.market_context}")
        if all(shown.values()):
            break


if __name__ == "__main__":
    main()
