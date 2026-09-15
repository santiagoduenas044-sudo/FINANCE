"""GHOST historical demonstration (Phase 14).

Runs the SAME brain used live over historical ES (and optionally NQ) sample
data, as-of each closed bar (no-lookahead), and narrates:

    context  ->  signal  ->  PaperTrade  ->  evolution  ->  close  ->  result (R)

plus one explained NO_TRADE. No broker, no orders, no external connection, no
AI/LLM — pure interpretation + simulated memory over data we already have.

    PYTHONPATH=. python scripts/demo_ghost.py [ES|NQ]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.ghost.brain import GhostBrain
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def _load(symbol: str, de: DataEngine):
    contract = "ESM26" if symbol == "ES" else None
    if contract is None:
        # NQ has no bundled sample; synthesize one from ES so the demo runs.
        es = normalize(CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26"),
                       de.cfg.data, "1m")
        for c in ("open", "high", "low", "close"):
            es[c] = es[c] * 3.6 + 12000.0
        return es
    return normalize(CsvProvider(SAMPLES).get_raw_bars(symbol, "1m", contract=contract),
                     de.cfg.data, "1m")


def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "ES").upper()
    de = DataEngine(); reg = ContractRegistry.load()
    se = SetupEngine(de.cfg, reg); sig = SignalEngine(de.cfg.signals)
    brain = GhostBrain(de.cfg.ghost)
    tracker = PaperTradeTracker()

    bars_1m = _load(symbol, de)
    # build multi-timeframe frames once (as-of slicing keeps it no-lookahead)
    from signal_engine.data.models import tf_to_timedelta
    from signal_engine.data.resample import resample_ohlcv
    from signal_engine.data.sessions import SessionClassifier
    ann = SessionClassifier(de.cfg.sessions).annotate(bars_1m, label_interval=tf_to_timedelta("1m"))
    tf = {"1m": ann}
    for t in de.cfg.data.target_timeframes:
        tf[t] = resample_ohlcv(ann, t, de.cfg.sessions, de.cfg.resample, de.cfg.data.timezone)
    exec_tf = de.cfg.timeframe_roles.execution
    times = list(tf[exec_tf].index)

    print(f"\n{'='*70}\n GHOST historical demo — {symbol}  ({len(times)} {exec_tf} bars)\n{'='*70}")

    first_trade_story = None
    no_trade_example = None
    evolutions: dict[str, list] = {}

    for N in times[60:]:
        ctx = de.analysis_context(symbol, N, tf)
        analysis = se.analyzer.analyze(ctx)
        bar = ctx.execution.bars.iloc[-1]
        # 1) update open trades with this closed bar, recording evolution
        for t in tracker.active(symbol):
            evolutions.setdefault(t.signal_id, []).append(
                (N, round(t.current_price, 2), round(t.pnl_points, 2), round(t.r_multiple, 2)))
        tracker.update_symbol(symbol, bar, N)
        # 2) decide
        base = sig.evaluate_candidates(ctx, se.detect(ctx))
        ms, decisions = brain.evaluate(ctx, analysis, base,
                                       has_active=tracker.has_open)
        for d in decisions:
            if d.is_trade:
                t = tracker.open_trade(
                    signal_id=d.signal.signal_id, instrument=symbol, direction=d.action,
                    setup=d.setup, entry=d.signal.entry, stop=d.signal.stop,
                    take_profits=list(d.signal.take_profits), opened_at=d.timestamp,
                    invalidation_price=d.signal.invalidation_price, timeframe=d.signal.timeframe,
                    context={"regime": ms.regime.regime.value, "session": ms.session,
                             "confluence": d.confluence.score})
                if t is not None and first_trade_story is None:
                    first_trade_story = (d, ms, t)
            elif no_trade_example is None and d.confluence is not None:
                no_trade_example = (d, ms)

    # ---- narrate the first trade ----
    if first_trade_story is not None:
        d, ms, t = first_trade_story
        print("\n--- 1) CONTEXT (as-of the decision bar) ---")
        print(f"  regime={ms.regime.regime.value} (vol {ms.regime.volatility.value})  "
              f"HTF={ms.mtf.context.bias.value}  15m={ms.mtf.structure.bias.value}  "
              f"5m={ms.mtf.execution.bias.value}  alignment={ms.mtf.alignment.value}")
        print(f"  VWAP {ms.vwap_side}  session={ms.session}  liquidity: {ms.liquidity.detail or '-'}")
        print("\n--- 2) SIGNAL ---")
        print(f"  {d.action} {symbol} {d.setup}  entry={t.entry:.2f}  SL={t.stop:.2f}  "
              f"TP={'/'.join(f'{x:.2f}' for x in t.take_profits)}  R:R={d.signal.risk_reward}")
        print(f"  CONFLUENCE score={d.confluence.score} / {d.confluence.threshold}")
        for c in d.confluence.contributions:
            print(f"     {'+' if c.points >= 0 else ''}{c.points:<6} {c.name} ({c.detail})")
        print("\n--- 3) PAPER TRADE + 4) EVOLUTION ---")
        ev = evolutions.get(t.signal_id, [])
        for (bt, px, pnl, r) in ev[:8]:
            print(f"     {bt}  px={px}  P&L={pnl:+} pts  {r:+.2f}R")
        if len(ev) > 8:
            print(f"     … ({len(ev)} bars total)")
        print("\n--- 5) CLOSE + 6) RESULT ---")
        print(f"  status={t.status.value}  exit={t.exit_price}  reason={t.exit_reason.value if t.exit_reason else '-'}")
        print(f"  RESULT: {t.r_multiple:+.2f}R   MFE={t.mfe_r:+.2f}R  MAE={t.mae_r:+.2f}R  "
              f"bars_held={t.bars_held}")
    else:
        print("\n(no GHOST-approved trade in this sample window)")

    # ---- narrate a NO_TRADE ----
    print("\n--- NO_TRADE example (explained) ---")
    if no_trade_example is not None:
        d, ms = no_trade_example
        print(f"  {d.setup} @ {d.timestamp}  ->  NO TRADE")
        print(f"  reasons: {[r.value for r in d.no_trade_reasons]}")
        print(f"  confluence score={d.confluence.score} / {d.confluence.threshold} "
              f"(regime {ms.regime.regime.value}, alignment {ms.mtf.alignment.value})")
    else:
        print("  (no NO_TRADE with a computed score in this window)")

    # ---- aggregate ----
    s = tracker.stats()
    print(f"\n--- TRADE HISTORY / STATS ---")
    print(f"  total={s['total_trades']} wins={s['wins']} losses={s['losses']} "
          f"win_rate={s['win_rate']} total_R={s['total_r']} avg_R={s['avg_r']}")
    print(f"  by_regime={s['by_regime']}")
    print(f"  by_setup={s['by_setup']}")
    print(f"\n{'='*70}")
    print(" no external connection · no broker · no orders · no AI in path · no-lookahead")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
