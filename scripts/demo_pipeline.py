"""End-to-end Data Engine demo on the sample CSVs.

Runs: CSV -> ingest -> normalize -> validate -> sessions -> multi-TF resample
-> continuous/roll -> Parquet storage -> AnalysisContext, printing examples of
processed data at each step. Infrastructure only; no strategy/indicators.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from signal_engine.data import storage
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.models import SESSION, TRADING_DATE
from signal_engine.data.providers.csv_provider import CsvProvider

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 12)

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def hr(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def main() -> None:
    engine = DataEngine()
    reg = ContractRegistry.load()
    provider = CsvProvider(SAMPLES)
    symbol, front, back = "ES", "ESM26", "ESU26"

    hr("0) ContractSpec (centralized config, NOT verified vs CME yet)")
    print(f"registry.verified = {reg.verified}  ({reg.verification.get('note','')})")
    spec = reg.spec("ES")
    print(f"ES  multiplier={spec.multiplier}  tick_size={spec.tick_size}  tick_value={spec.tick_value}")

    hr("1) Load + normalize + validate + session-annotate (base 1m, front)")
    lc = engine.load_contract(provider, symbol, front, "1m")
    print("validation:", lc.report.summary())
    print("base 1m head:")
    print(lc.bars.head(3)[["open", "high", "low", "close", "volume", SESSION, TRADING_DATE]])
    print("\nsession bar counts:")
    print(lc.bars[SESSION].value_counts())

    hr("2) Multi-timeframe resample (no look-ahead, session-aware)")
    engine.build_timeframes(lc)
    for tf in ("5m", "15m", "1H"):
        df = lc.timeframes[tf]
        print(f"\n{tf}: {len(df)} bars; first 2 RTH bars:")
        rth = df[df[SESSION] == "RTH"]
        print(rth.head(2)[["open", "high", "low", "close", "volume", SESSION]])

    hr("3) Validation demo on deliberately-invalid file")
    bad = engine.load_contract(provider, symbol, "BAD", "1m")
    print(bad.report.summary())

    hr("4) Rollover schedule + continuous series")
    cont, events = engine.build_continuous(provider, symbol, [front, back], "1m")
    for e in events:
        print(f"roll {e.from_contract} -> {e.to_contract} on {e.roll_date} (ratio={e.ratio:.5f})")
    roll_day = events[0].roll_date
    around = cont[(cont[TRADING_DATE] >= roll_day)]
    print("\ncontinuous (ratio-adjusted) first bars of roll day:")
    print(around.head(2)[["open", "close", "volume", "contract", TRADING_DATE]])

    hr("5) Parquet storage + reproducibility")
    engine.cfg.storage.root = str(ROOT / "data_store")
    engine.store_timeframes(lc)
    for tf in ("1m", "5m", "15m", "1H"):
        h1 = storage.content_hash(lc.timeframes[tf])
        meta = storage.read_meta(engine.cfg.storage.root, symbol, front, tf)
        print(f"{tf}: rows={meta['rows']} hash={h1[:16]}...  (meta match={meta['content_hash']==h1})")

    hr("6) AnalysisContext (as-of decision time)")
    N = pd.Timestamp("2026-06-08 11:07", tz="America/New_York")
    ctx = engine.analysis_context(symbol, N, lc.timeframes)
    print(f"decision_time={ctx.decision_time}  session={ctx.session}")
    print(f"htf({ctx.htf.timeframe}) last closed bar: {ctx.htf.bars.index[-1]}")
    print(f"structure({ctx.structure.timeframe}) last closed bar: {ctx.structure.bars.index[-1]}")
    print(f"execution({ctx.execution.timeframe}) last closed bar: {ctx.execution.bars.index[-1]}")
    print("(no bar closes after N in any view -> no look-ahead)")


if __name__ == "__main__":
    main()
