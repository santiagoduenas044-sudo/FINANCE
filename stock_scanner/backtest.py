"""Walk-forward backtest of the ranking.

Every `rebalance_days` bars: run the scanner on history truncated to that date
(point-in-time, no lookahead), buy the top N names equal-weight at that close,
hold until the next rebalance. Compared against the benchmark and against an
equal-weight portfolio of the whole (filtered) universe.

Caveats worth reading before trusting a number: the universe is today's list
of large caps (survivorship bias flatters results), fills are at the close,
and costs are a flat `cost_bps` per side on the names that change.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .scanner import BUY, StockScanner, rank_as_of


@dataclass
class BacktestResult:
    periods: pd.DataFrame            # one row per holding period
    equity: pd.DataFrame             # cumulative growth of 1: strategy/universe/benchmark
    metrics: dict[str, dict[str, float]]


def _price_at(df: pd.DataFrame, date: pd.Timestamp) -> float:
    s = df["close"].loc[:date]
    return float(s.iloc[-1]) if len(s) else float("nan")


def _metrics(rets: pd.Series, periods_per_year: float) -> dict[str, float]:
    rets = rets.fillna(0.0)
    growth = (1 + rets).cumprod()
    years = len(rets) / periods_per_year
    total = float(growth.iloc[-1] - 1) if len(growth) else 0.0
    cagr = float(growth.iloc[-1] ** (1 / years) - 1) if years > 0 and growth.iloc[-1] > 0 else float("nan")
    vol = float(rets.std(ddof=1) * np.sqrt(periods_per_year)) if len(rets) > 1 else float("nan")
    return {
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": float(rets.mean() * periods_per_year / vol) if vol and vol > 0 else float("nan"),
        "max_drawdown": float((growth / growth.cummax() - 1).min()) if len(growth) else 0.0,
        "win_rate": float((rets > 0).mean()) if len(rets) else float("nan"),
    }


def run_backtest(histories: dict[str, pd.DataFrame], scanner: StockScanner, top_n: int = 10,
                 rebalance_days: int = 21, warmup_bars: int = 260, cost_bps: float = 10.0,
                 only_buy_signals: bool = True) -> BacktestResult:
    bench_sym = scanner.config.benchmark
    calendar_src = histories.get(bench_sym)
    if calendar_src is None:
        calendar_src = max(histories.values(), key=len)
    calendar = calendar_src.index
    if len(calendar) <= warmup_bars + rebalance_days:
        raise ValueError(f"need more than {warmup_bars + rebalance_days} bars of history, have {len(calendar)}")

    dates = list(calendar[warmup_bars::rebalance_days])
    if dates[-1] != calendar[-1]:
        dates.append(calendar[-1])

    rows = []
    prev: set[str] = set()
    for start, end in zip(dates[:-1], dates[1:]):
        result = rank_as_of(histories, start, scanner)
        pool = result.buys if only_buy_signals else result.opportunities
        picks = [o.ticker for o in pool[:top_n]]

        def fwd(t: str) -> float:
            p0, p1 = _price_at(histories[t], start), _price_at(histories[t], end)
            return p1 / p0 - 1 if p0 > 0 else float("nan")

        pick_rets = [fwd(t) for t in picks]
        universe_rets = [fwd(o.ticker) for o in result.opportunities]
        turnover = len(set(picks) ^ prev) / max(len(picks), len(prev), 1)
        gross = float(np.nanmean(pick_rets)) if picks else 0.0   # no picks => cash
        cost = turnover * cost_bps / 1e4 if picks or prev else 0.0
        rows.append({
            "start": start, "end": end, "picks": ", ".join(picks), "n_picks": len(picks),
            "strategy": gross - cost,
            "universe": float(np.nanmean(universe_rets)) if universe_rets else 0.0,
            "benchmark": fwd(bench_sym) if bench_sym in histories else float("nan"),
            "turnover": turnover,
        })
        prev = set(picks)

    periods = pd.DataFrame(rows).set_index("start")
    equity = (1 + periods[["strategy", "universe", "benchmark"]].fillna(0.0)).cumprod()
    ppy = 252 / rebalance_days
    metrics = {c: _metrics(periods[c], ppy) for c in ("strategy", "universe", "benchmark")}
    metrics["strategy"]["beat_benchmark_pct"] = float((periods["strategy"] > periods["benchmark"]).mean())
    metrics["strategy"]["avg_picks"] = float(periods["n_picks"].mean())
    metrics["strategy"]["avg_turnover"] = float(periods["turnover"].mean())
    return BacktestResult(periods, equity, metrics)
