"""Market Analysis demo (Phase 4). Builds an as-of AnalysisContext from the
sample CSVs and runs the MarketAnalyzer with BOTH backends (native + smc),
showing indicators and market-structure objects. Analysis tools only — no
strategy, signals, risk, or IA.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.features.analyzer import MarketAnalyzer

pd.set_option("display.width", 130)
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def hr(t):
    print("\n" + "=" * 68 + f"\n{t}\n" + "=" * 68)


def show(ta, name):
    print(f"\n[{name}] timeframe={ta.timeframe}  bars<=N={len(ta.indicators)}")
    last = ta.indicators.dropna().tail(1)
    if not last.empty:
        row = last.iloc[0]
        cols = [c for c in ta.indicators.columns]
        print("  last indicators:", {c: round(float(row[c]), 2) for c in cols})
    print(f"  swings={len(ta.swings)} structure={len(ta.structure_events)} "
          f"fvg={len(ta.fair_value_gaps)} liq={len(ta.liquidity_levels)} ob={len(ta.order_blocks)}")
    for s in ta.swings[-3:]:
        print(f"    swing {s.timestamp:%m-%d %H:%M} {s.type.value:4} "
              f"{s.price:.2f} {s.label.value if s.label else '-'}")
    for e in ta.structure_events[-2:]:
        print(f"    struct {e.timestamp:%m-%d %H:%M} {e.type.value} {e.direction.value} @ {e.level:.2f}")


def main():
    engine = DataEngine()
    provider = CsvProvider(SAMPLES)
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    engine.build_timeframes(lc)
    N = pd.Timestamp("2026-06-08 14:30", tz="America/New_York")
    ctx = engine.analysis_context("ES", N, lc.timeframes)
    print(f"AnalysisContext symbol=ES decision_time={ctx.decision_time} session={ctx.session}")

    for backend, kwargs in (("native", {"n_left": 3, "n_right": 3}),
                            ("smc", {"swing_length": 8})):
        hr(f"Backend: {backend}")
        analyzer = MarketAnalyzer(backend=backend, provider_kwargs=kwargs)
        result = analyzer.analyze(ctx)
        for tf in (ctx.execution.timeframe, ctx.structure.timeframe, ctx.htf.timeframe):
            show(result.by_timeframe[tf], tf)


if __name__ == "__main__":
    main()
