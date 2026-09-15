"""Metrics computation (Phase 8). Pure function of a list[ClosedTrade].

R-multiple metrics are always computed (idealized, cost-free — see
docs/PHASE8_BACKTESTING.md). Dollar aggregates are computed only when EVERY
resolved (WIN/LOSS) trade carries a `.dollar` result — i.e. only when the
whole run used a verified ContractRegistry via the RiskEngine.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .models import BacktestMetrics, ClosedTrade, GroupStats, TradeOutcome


def _group(trades: list[ClosedTrade], key) -> dict[str, GroupStats]:
    buckets: dict[str, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        if t.outcome in (TradeOutcome.WIN, TradeOutcome.LOSS):
            buckets[key(t)].append(t)
    out = {}
    for k, group in buckets.items():
        wins = [t for t in group if t.outcome is TradeOutcome.WIN]
        losses = [t for t in group if t.outcome is TradeOutcome.LOSS]
        n = len(group)
        out[k] = GroupStats(
            trades=n, wins=len(wins), losses=len(losses),
            win_rate=len(wins) / n if n else 0.0,
            net_r=sum(t.r_multiple for t in group if t.r_multiple is not None),
        )
    return out


def _max_streak(outcomes: list[TradeOutcome], target: TradeOutcome) -> int:
    best = cur = 0
    for o in outcomes:
        if o is target:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _max_drawdown(cumulative: list[float]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for v in cumulative:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    return max_dd


def compute_metrics(trades: list[ClosedTrade], no_trade_count: int = 0) -> BacktestMetrics:
    total_signals = len(trades)
    resolved = [t for t in trades
               if t.outcome in (TradeOutcome.WIN, TradeOutcome.LOSS)]
    wins = [t for t in resolved if t.outcome is TradeOutcome.WIN]
    losses = [t for t in resolved if t.outcome is TradeOutcome.LOSS]
    expired = sum(1 for t in trades if t.outcome is TradeOutcome.EXPIRED)
    open_trades = sum(1 for t in trades if t.outcome is TradeOutcome.OPEN)

    n = len(resolved)
    win_rate = len(wins) / n if n else 0.0
    r_values = [t.r_multiple for t in resolved]
    win_r = [t.r_multiple for t in wins]
    loss_r = [abs(t.r_multiple) for t in losses]   # magnitude

    avg_win_r = sum(win_r) / len(win_r) if win_r else 0.0
    avg_loss_r = sum(loss_r) / len(loss_r) if loss_r else 0.0
    gross_win = sum(win_r)
    gross_loss = sum(loss_r)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    expectancy_r = sum(r_values) / n if n else 0.0
    net_r = sum(r_values)

    # ordered by entry time for streaks / drawdown (resolved trades only)
    ordered = sorted(resolved, key=lambda t: t.entry_timestamp)
    cumulative: list[float] = []
    running = 0.0
    for t in ordered:
        running += t.r_multiple
        cumulative.append(running)
    max_dd_r = _max_drawdown(cumulative) if cumulative else 0.0

    mean_r = expectancy_r
    if n > 1:
        variance = sum((r - mean_r) ** 2 for r in r_values) / (n - 1)
        std_r = math.sqrt(variance)
    else:
        std_r = 0.0
    sharpe_r = (mean_r / std_r) if std_r > 0 else 0.0

    downside = [min(0.0, r - mean_r) for r in r_values]
    if n > 1 and any(d != 0 for d in downside):
        downside_var = sum(d ** 2 for d in downside) / (n - 1)
        downside_std = math.sqrt(downside_var)
    else:
        downside_std = 0.0
    sortino_r = (mean_r / downside_std) if downside_std > 0 else 0.0

    ordered_outcomes = [t.outcome for t in ordered]
    max_consec_wins = _max_streak(ordered_outcomes, TradeOutcome.WIN)
    max_consec_losses = _max_streak(ordered_outcomes, TradeOutcome.LOSS)

    by_instrument = _group(trades, lambda t: t.instrument)
    by_setup = _group(trades, lambda t: t.setup)
    by_direction = _group(trades, lambda t: t.direction.value)

    # dollar aggregates: only if every resolved trade has a `.dollar` result
    dollar_trades = [t for t in resolved if t.dollar is not None]
    net_pnl_dollars = max_dd_dollars = avg_win_dollars = avg_loss_dollars = None
    total_commission = None
    dollar_reason = None
    if resolved and len(dollar_trades) == len(resolved):
        pnls = [t.dollar.net_pnl for t in dollar_trades]
        win_pnls = [t.dollar.net_pnl for t in dollar_trades if t.outcome is TradeOutcome.WIN]
        loss_pnls = [t.dollar.net_pnl for t in dollar_trades if t.outcome is TradeOutcome.LOSS]
        net_pnl_dollars = sum(pnls)
        avg_win_dollars = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss_dollars = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
        total_commission = sum(t.dollar.commission_total for t in dollar_trades)
        ordered_dollar = sorted(dollar_trades, key=lambda t: t.entry_timestamp)
        cum_pnl, running_pnl = [], 0.0
        for t in ordered_dollar:
            running_pnl += t.dollar.net_pnl
            cum_pnl.append(running_pnl)
        max_dd_dollars = _max_drawdown(cum_pnl) if cum_pnl else 0.0
    elif resolved:
        dollar_reason = (
            "Dollar metrics require every resolved trade's Signal to be "
            "APPROVED by a RiskEngine backed by a VERIFIED ContractRegistry; "
            "see docs/CME_VERIFICATION.md. R-multiple metrics above are "
            "unaffected and always valid."
        )

    return BacktestMetrics(
        total_signals=total_signals, no_trade_count=no_trade_count,
        resolved_trades=n, wins=len(wins), losses=len(losses),
        expired=expired, open_trades=open_trades, win_rate=win_rate,
        avg_win_r=avg_win_r, avg_loss_r=avg_loss_r, profit_factor=profit_factor,
        expectancy_r=expectancy_r, net_r=net_r, max_drawdown_r=max_dd_r,
        sharpe_r=sharpe_r, sortino_r=sortino_r,
        max_consecutive_wins=max_consec_wins, max_consecutive_losses=max_consec_losses,
        by_instrument=by_instrument, by_setup=by_setup, by_direction=by_direction,
        net_pnl_dollars=net_pnl_dollars, max_drawdown_dollars=max_dd_dollars,
        avg_win_dollars=avg_win_dollars, avg_loss_dollars=avg_loss_dollars,
        total_commission_dollars=total_commission, dollar_metrics_reason=dollar_reason,
    )
