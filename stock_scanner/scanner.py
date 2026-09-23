"""Rank a universe by composite score and build trade plans.

Pipeline:  history -> factors (per ticker) -> hard filters -> cross-sectional
percentiles -> weighted score (0-100) -> signal + setup label -> trade plan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .config import ScannerConfig
from .data import DataProvider
from .factors import Factors, compute_factors

BUY, WATCH, AVOID = "COMPRAR", "VIGILAR", "EVITAR"


@dataclass
class TradePlan:
    entry: float
    stop: float
    target: float
    risk_per_share: float
    shares: int
    position_value: float
    capital_at_risk: float
    reward_risk: float
    upside_to_52w_high_pct: float


@dataclass
class Opportunity:
    rank: int
    ticker: str
    score: float
    signal: str
    setup: str
    reasons: list[str]
    components: dict[str, float]
    factors: Factors
    plan: TradePlan | None

    def as_dict(self) -> dict:
        f = self.factors.as_dict()
        f["date"] = str(f["date"].date())
        return {
            "rank": self.rank, "ticker": self.ticker, "score": round(self.score, 1),
            "signal": self.signal, "setup": self.setup, "reasons": self.reasons,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "factors": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()},
            "plan": None if self.plan is None else {k: round(v, 2) if isinstance(v, float) else v
                                                     for k, v in self.plan.__dict__.items()},
        }


@dataclass
class ScanResult:
    as_of: pd.Timestamp | None
    opportunities: list[Opportunity]
    rejected: dict[str, str] = field(default_factory=dict)
    benchmark_ret_6m: float = float("nan")

    @property
    def buys(self) -> list[Opportunity]:
        return [o for o in self.opportunities if o.signal == BUY]

    def top(self, n: int) -> list[Opportunity]:
        return self.opportunities[:n]


def _pct_rank(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """Percentile in (0, 1]; NaN -> neutral 0.5 so missing data neither helps nor hurts."""
    r = values.rank(pct=True, ascending=higher_is_better)
    return r.fillna(0.5)


def score_factors(factors: list[Factors], weights: dict[str, float]) -> pd.DataFrame:
    """Composite score per ticker. Returns a frame indexed by ticker with one
    0..1 column per component plus `score` (0-100)."""
    df = pd.DataFrame([f.as_dict() for f in factors]).set_index("ticker")
    comp = pd.DataFrame(index=df.index)
    comp["momentum"] = (_pct_rank(df["mom_12_1"]) + _pct_rank(df["ret_6m"])) / 2
    comp["relative_strength"] = _pct_rank(df["rel_strength_6m"])
    comp["trend"] = df["trend"].fillna(0.5)
    comp["high_proximity"] = _pct_rank(df["high_proximity"])
    comp["accumulation"] = _pct_rank(df["accumulation"])
    comp["timing"] = df["timing"].fillna(0.5)
    comp["low_volatility"] = _pct_rank(df["vol_60d"], higher_is_better=False)
    comp["score"] = 100 * sum(comp[k] * w for k, w in weights.items())
    return comp


def classify_setup(f: Factors, overbought: float) -> str:
    above200 = not math.isnan(f.sma200) and f.close > f.sma200
    if not math.isnan(f.sma200) and f.close < f.sma200 and f.sma50 < f.sma200:
        return "Tendencia bajista"
    if f.rsi14 >= overbought or f.ext_atr > 3.5:
        return "Sobreextendida"
    if f.high_proximity >= 0.98 and f.volume_ratio >= 1.1:
        return "Ruptura de máximos"
    if f.trend >= 0.75 and -1.0 <= f.ext_atr <= 1.0 and 38 <= f.rsi14 <= 60:
        return "Retroceso en tendencia"
    if above200 and f.rsi14 < 35:
        return "Rebote sobrevendido"
    if f.trend >= 0.75:
        return "Momentum alcista"
    return "Neutral"


def explain(f: Factors, comp: pd.Series) -> list[str]:
    """Human-readable reasons, strongest first."""
    out: list[str] = []
    if f.trend >= 1.0:
        out.append("Tendencia alcista completa (precio > SMA50 > SMA200, SMA50 subiendo)")
    elif not math.isnan(f.sma200) and f.close < f.sma200:
        out.append("Precio por debajo de la SMA200")
    if comp["momentum"] >= 0.8:
        out.append(f"Momentum top 20% (6m {f.ret_6m:+.0%}, 12-1m {f.mom_12_1:+.0%})")
    if not math.isnan(f.rel_strength_6m):
        tag = "supera" if f.rel_strength_6m > 0 else "va por detrás de"
        out.append(f"{tag.capitalize()} al índice por {abs(f.rel_strength_6m):.0%} en 6m")
    if f.high_proximity >= 0.95:
        out.append(f"A {1 - f.high_proximity:.1%} del máximo de 52 semanas")
    if not math.isnan(f.accumulation) and f.accumulation >= 1.3:
        out.append(f"Acumulación: volumen en días alcistas {f.accumulation:.1f}x el de días bajistas")
    if f.rsi14 >= 70:
        out.append(f"RSI {f.rsi14:.0f}: sobrecomprada, mejor esperar retroceso")
    elif f.rsi14 <= 30:
        out.append(f"RSI {f.rsi14:.0f}: sobrevendida")
    if f.ext_atr > 3:
        out.append(f"Estirada {f.ext_atr:.1f} ATR sobre la EMA20")
    return out


def build_plan(f: Factors, risk) -> TradePlan | None:
    if math.isnan(f.atr14) or f.atr14 <= 0:
        return None
    entry = f.close
    stop = entry - risk.atr_stop_mult * f.atr14
    if stop <= 0:
        return None
    rps = entry - stop
    target = entry + risk.reward_risk * rps
    by_risk = risk.capital * risk.risk_per_trade_pct / 100 / rps
    by_cap = risk.capital * risk.max_position_pct / 100 / entry
    shares = int(max(0, math.floor(min(by_risk, by_cap))))
    return TradePlan(
        entry=entry, stop=stop, target=target, risk_per_share=rps, shares=shares,
        position_value=shares * entry, capital_at_risk=shares * rps,
        reward_risk=risk.reward_risk,
        upside_to_52w_high_pct=(f.high_52w / entry - 1) * 100,
    )


class StockScanner:
    def __init__(self, config: ScannerConfig, provider: DataProvider | None = None):
        self.config = config
        self.provider = provider

    # -- data --------------------------------------------------------------
    def fetch(self, tickers: list[str] | None = None) -> dict[str, pd.DataFrame]:
        if self.provider is None:
            raise RuntimeError("no data provider configured")
        tickers = list(tickers or self.config.universe)
        return self.provider.history_many(tickers + [self.config.benchmark])

    # -- analysis ----------------------------------------------------------
    def scan(self, histories: dict[str, pd.DataFrame] | None = None,
             tickers: list[str] | None = None) -> ScanResult:
        if histories is None:
            histories = self.fetch(tickers)
        cfg = self.config
        bench = histories.get(cfg.benchmark)
        bench_close = bench["close"] if bench is not None else None
        wanted = [t for t in (tickers or cfg.universe) if t != cfg.benchmark]

        factors: list[Factors] = []
        rejected: dict[str, str] = {}
        for t in wanted:
            df = histories.get(t)
            reason = self._reject_reason(df)
            if reason is None:
                f = compute_factors(t, df, bench_close, cfg.signals.rsi_overbought)
                reason = self._filter_factors(f)
            if reason:
                rejected[t] = reason
            else:
                factors.append(f)

        bench_6m = float("nan")
        if bench_close is not None and len(bench_close) > 126:
            bench_6m = float(bench_close.iloc[-1] / bench_close.iloc[-127] - 1)
        if not factors:
            return ScanResult(None, [], rejected, bench_6m)

        comp = score_factors(factors, cfg.weights).sort_values("score", ascending=False)
        by_ticker = {f.ticker: f for f in factors}
        opps = []
        for rank, (ticker, row) in enumerate(comp.iterrows(), start=1):
            f = by_ticker[ticker]
            signal = self._signal(f, row["score"])
            opps.append(Opportunity(
                rank=rank, ticker=ticker, score=float(row["score"]), signal=signal,
                setup=classify_setup(f, cfg.signals.rsi_overbought),
                reasons=explain(f, row),
                components={k: float(row[k]) for k in cfg.weights},
                factors=f,
                plan=build_plan(f, cfg.risk) if signal != AVOID else None,
            ))
        as_of = max(f.date for f in factors)
        return ScanResult(as_of, opps, rejected, bench_6m)

    def _reject_reason(self, df: pd.DataFrame | None) -> str | None:
        if df is None or df.empty:
            return "sin datos"
        if len(df) < self.config.filters.min_history_bars:
            return f"historial insuficiente ({len(df)} barras)"
        return None

    def _filter_factors(self, f: Factors) -> str | None:
        flt = self.config.filters
        if f.close < flt.min_price:
            return f"precio {f.close:.2f} < {flt.min_price}"
        if not (f.avg_dollar_volume >= flt.min_avg_dollar_volume):
            return f"liquidez ${f.avg_dollar_volume / 1e6:.1f}M/día < ${flt.min_avg_dollar_volume / 1e6:.0f}M"
        return None

    def _signal(self, f: Factors, score: float) -> str:
        rules = self.config.signals
        above200 = not math.isnan(f.sma200) and f.close > f.sma200
        if score >= rules.buy_score and above200 and f.rsi14 < rules.rsi_overbought:
            return BUY
        if score >= rules.watch_score:
            return WATCH
        return AVOID


def rank_as_of(histories: dict[str, pd.DataFrame], date: pd.Timestamp,
               scanner: StockScanner) -> ScanResult:
    """Point-in-time scan: every frame truncated to bars <= `date`."""
    cut = {t: df.loc[:date] for t, df in histories.items()}
    return scanner.scan(cut)


__all__ = ["BUY", "WATCH", "AVOID", "Opportunity", "ScanResult", "StockScanner",
           "TradePlan", "build_plan", "classify_setup", "rank_as_of", "score_factors"]
