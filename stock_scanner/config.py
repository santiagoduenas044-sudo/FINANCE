"""Scanner configuration (YAML -> dataclasses)."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")


@dataclass
class Filters:
    min_price: float = 5.0
    min_avg_dollar_volume: float = 20e6
    min_history_bars: int = 210


@dataclass
class SignalRules:
    buy_score: float = 70.0
    watch_score: float = 55.0
    rsi_overbought: float = 75.0


@dataclass
class RiskSettings:
    capital: float = 10_000.0
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0
    atr_stop_mult: float = 2.0
    reward_risk: float = 2.0


DEFAULT_WEIGHTS = {
    "momentum": 0.25, "relative_strength": 0.20, "trend": 0.20, "high_proximity": 0.10,
    "accumulation": 0.10, "timing": 0.10, "low_volatility": 0.05,
}


@dataclass
class ScannerConfig:
    universe: list[str] = field(default_factory=list)
    benchmark: str = "SPY"
    filters: Filters = field(default_factory=Filters)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    signals: SignalRules = field(default_factory=SignalRules)
    risk: RiskSettings = field(default_factory=RiskSettings)

    def __post_init__(self) -> None:
        unknown = set(self.weights) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown weight keys: {sorted(unknown)}")
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("weights must sum to a positive number")
        self.weights = {k: v / total for k, v in self.weights.items()}
        self.universe = [t.strip().upper() for t in self.universe if t.strip()]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ScannerConfig":
        raw = yaml.safe_load(Path(path or DEFAULT_CONFIG).read_text()) or {}

        def _sub(klass, key):
            data = raw.get(key) or {}
            allowed = {f.name for f in fields(klass)}
            bad = set(data) - allowed
            if bad:
                raise ValueError(f"unknown keys in '{key}': {sorted(bad)}")
            return klass(**data)

        return cls(
            universe=[str(t) for t in raw.get("universe", [])],
            benchmark=str(raw.get("benchmark", "SPY")).upper(),
            filters=_sub(Filters, "filters"),
            weights=raw.get("weights") or dict(DEFAULT_WEIGHTS),
            signals=_sub(SignalRules, "signals"),
            risk=_sub(RiskSettings, "risk"),
        )
