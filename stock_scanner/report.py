"""Render scan and backtest results as console text, Markdown or JSON."""
from __future__ import annotations

import json
import math

from .backtest import BacktestResult
from .scanner import BUY, WATCH, ScanResult

DISCLAIMER = ("Herramienta de análisis, no asesoría financiera. Verifica los datos y "
              "gestiona tu riesgo antes de operar.")


def _pct(v: float, digits: int = 1) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:+.{digits}f}%"


def _num(v: float, digits: int = 2) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:,.{digits}f}"


def ranking_table(result: ScanResult, top: int) -> str:
    head = (f"{'#':>3} {'Ticker':<7} {'Score':>5} {'Señal':<8} {'Setup':<23} "
            f"{'Precio':>9} {'6m':>7} {'vs idx':>7} {'RSI':>4} {'Entrada':>9} {'Stop':>9} "
            f"{'Objetivo':>9} {'Accs':>5}")
    lines = [head, "-" * len(head)]
    for o in result.top(top):
        f, p = o.factors, o.plan
        lines.append(
            f"{o.rank:>3} {o.ticker:<7} {o.score:>5.1f} {o.signal:<8} {o.setup:<23.23} "
            f"{_num(f.close):>9} {_pct(f.ret_6m, 0):>7} {_pct(f.rel_strength_6m, 0):>7} "
            f"{_num(f.rsi14, 0):>4} "
            f"{_num(p.entry) if p else '—':>9} {_num(p.stop) if p else '—':>9} "
            f"{_num(p.target) if p else '—':>9} {p.shares if p else '—':>5}")
    return "\n".join(lines)


def to_console(result: ScanResult, top: int = 15, details: int = 5) -> str:
    if not result.opportunities:
        out = ["No hay acciones que pasen los filtros."]
        out += [f"  {t}: {r}" for t, r in result.rejected.items()]
        return "\n".join(out)
    out = [
        f"ESCÁNER DE OPORTUNIDADES — datos al {result.as_of.date()}",
        f"Analizadas: {len(result.opportunities)}  |  COMPRAR: {len(result.buys)}  |  "
        f"Índice 6m: {_pct(result.benchmark_ret_6m)}",
        "",
        ranking_table(result, top),
        "",
    ]
    featured = [o for o in result.opportunities if o.signal in (BUY, WATCH)][:details]
    if featured:
        out.append("MEJORES OPORTUNIDADES — por qué")
    for o in featured:
        out.append(f"\n  {o.rank}. {o.ticker}  score {o.score:.1f}  [{o.signal}]  {o.setup}")
        for r in o.reasons:
            out.append(f"     • {r}")
        if o.plan:
            p = o.plan
            out.append(f"     Plan: entrada {p.entry:,.2f} · stop {p.stop:,.2f} "
                       f"(-{p.risk_per_share / p.entry:.1%}) · objetivo {p.target:,.2f} "
                       f"(+{p.reward_risk * p.risk_per_share / p.entry:.1%}) · "
                       f"{p.shares} acciones (${p.position_value:,.0f}, riesgo ${p.capital_at_risk:,.0f})")
    if result.rejected:
        out.append(f"\nDescartadas ({len(result.rejected)}): " +
                   "; ".join(f"{t} ({r})" for t, r in list(result.rejected.items())[:10]))
    out.append(f"\n{DISCLAIMER}")
    return "\n".join(out)


def to_markdown(result: ScanResult, top: int = 20) -> str:
    out = [f"# Oportunidades — {result.as_of.date() if result.as_of else 'sin datos'}", "",
           f"COMPRAR: **{len(result.buys)}** de {len(result.opportunities)} analizadas · "
           f"índice 6m {_pct(result.benchmark_ret_6m)}", "",
           "| # | Ticker | Score | Señal | Setup | Precio | 6m | vs índice | RSI | Entrada | Stop | Objetivo | Acciones |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for o in result.top(top):
        f, p = o.factors, o.plan
        out.append(f"| {o.rank} | **{o.ticker}** | {o.score:.1f} | {o.signal} | {o.setup} | "
                   f"{_num(f.close)} | {_pct(f.ret_6m, 0)} | {_pct(f.rel_strength_6m, 0)} | "
                   f"{_num(f.rsi14, 0)} | {_num(p.entry) if p else '—'} | {_num(p.stop) if p else '—'} | "
                   f"{_num(p.target) if p else '—'} | {p.shares if p else '—'} |")
    out += ["", "## Detalle", ""]
    for o in result.opportunities[:top]:
        if o.signal not in (BUY, WATCH):
            continue
        out.append(f"### {o.ticker} — {o.score:.1f} ({o.signal}, {o.setup})")
        out += [f"- {r}" for r in o.reasons]
        out.append("")
    out.append(f"_{DISCLAIMER}_")
    return "\n".join(out)


def to_json(result: ScanResult, backtest: BacktestResult | None = None) -> str:
    payload = {
        "as_of": str(result.as_of.date()) if result.as_of else None,
        "benchmark_ret_6m": result.benchmark_ret_6m,
        "opportunities": [o.as_dict() for o in result.opportunities],
        "rejected": result.rejected,
    }
    if backtest is not None:
        payload["backtest"] = backtest.metrics
    return json.dumps(_nan_to_none(payload), indent=2, ensure_ascii=False, default=str)


def _nan_to_none(obj):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nan_to_none(v) for v in obj]
    return obj


def backtest_console(bt: BacktestResult) -> str:
    labels = {"strategy": "Estrategia (top N)", "universe": "Universo equiponderado",
              "benchmark": "Índice de referencia"}
    head = f"{'':<24} {'Total':>8} {'CAGR':>8} {'Vol':>7} {'Sharpe':>7} {'MaxDD':>8} {'% meses +':>9}"
    out = [f"BACKTEST WALK-FORWARD — {len(bt.periods)} periodos "
           f"({bt.periods.index[0].date()} → {bt.periods['end'].iloc[-1].date()})", head, "-" * len(head)]
    for key, label in labels.items():
        m = bt.metrics[key]
        out.append(f"{label:<24} {_pct(m['total_return']):>8} {_pct(m['cagr']):>8} "
                   f"{_num(m['volatility'] * 100, 0) + '%':>7} {_num(m['sharpe']):>7} {_pct(m['max_drawdown']):>8} "
                   f"{m['win_rate'] * 100:>8.0f}%")
    s = bt.metrics["strategy"]
    out.append(f"\nSupera al índice en {s['beat_benchmark_pct']:.0%} de los periodos · "
               f"{s['avg_picks']:.1f} acciones de media · rotación {s['avg_turnover']:.0%}")
    out.append("Ojo: universo actual (sesgo de supervivencia), ejecución al cierre, costes simplificados.")
    return "\n".join(out)
