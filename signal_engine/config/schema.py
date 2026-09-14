"""Typed configuration models (pydantic v2) for the Data Engine.

Config is loaded from YAML and validated here. Nothing in the strategy/risk
logic should hardcode values that belong in configuration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Data Engine config
# --------------------------------------------------------------------------- #
class DataConfig(BaseModel):
    timezone: str = "America/New_York"
    base_timeframe: str = "1m"
    target_timeframes: list[str] = Field(default_factory=lambda: ["5m", "15m", "1H"])
    input_timestamp_convention: Literal["open", "close"] = "open"
    dst_ambiguous: str = "infer"
    dst_nonexistent: str = "shift_forward"


class ValidationConfig(BaseModel):
    gap_factor: float = 3.0
    drop_invalid: bool = False
    raise_on_error: bool = False


class RthConfig(BaseModel):
    start: str = "09:30"
    end: str = "16:00"


class MaintenanceBreak(BaseModel):
    start: str = "17:00"
    end: str = "18:00"


class EthConfig(BaseModel):
    enabled: bool = True
    start: str = "18:00"
    end: str = "17:00"
    maintenance_break: MaintenanceBreak = Field(default_factory=MaintenanceBreak)


class SessionsConfig(BaseModel):
    rth: RthConfig = Field(default_factory=RthConfig)
    eth: EthConfig = Field(default_factory=EthConfig)
    trade_date_rollover: str = "17:00"


class ResampleConfig(BaseModel):
    origin: Literal["session_open", "epoch"] = "session_open"


class RolloverConfig(BaseModel):
    method: Literal["volume"] = "volume"
    volume_ratio: float = 1.0
    lookback_days: int = 15
    continuous_adjustment: Literal["raw", "ratio"] = "ratio"


class StorageConfig(BaseModel):
    root: str = "data_store"


class TimeframeRoles(BaseModel):
    context: str = "1H"
    structure: str = "15m"
    execution: str = "5m"


class AnalysisConfig(BaseModel):
    """Market Analysis (Phase 4) backend selection."""
    structure_backend: Literal["native", "smc"] = "native"
    native_n_left: int = 3
    native_n_right: int = 3
    smc_swing_length: int = 8
    with_order_blocks: bool = True


class LiquiditySweepParams(BaseModel):
    enabled: bool = True
    timeframe: str = "5m"
    stop_buffer_ticks: float = 2.0
    rr_targets: list[float] = Field(default_factory=lambda: [1.0, 2.0])
    expiration_bars: int = 6
    use_opposing_liquidity_target: bool = True


class VwapReclaimParams(BaseModel):
    enabled: bool = True
    timeframe: str = "5m"
    lookback_bars: int = 12
    stop_buffer_ticks: float = 2.0
    rr_targets: list[float] = Field(default_factory=lambda: [1.0, 2.0])
    expiration_bars: int = 6


class OrbParams(BaseModel):
    enabled: bool = True
    timeframe: str = "5m"
    opening_range_minutes: int = 30
    session: str = "RTH"
    rr_targets: list[float] = Field(default_factory=lambda: [1.0, 2.0])  # multiples of OR range
    expiration_bars: int = 12


class SetupsConfig(BaseModel):
    liquidity_sweep: LiquiditySweepParams = Field(default_factory=LiquiditySweepParams)
    vwap_reclaim: VwapReclaimParams = Field(default_factory=VwapReclaimParams)
    orb: OrbParams = Field(default_factory=OrbParams)


class EngineConfig(BaseModel):
    version: int = 1
    data: DataConfig = Field(default_factory=DataConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    resample: ResampleConfig = Field(default_factory=ResampleConfig)
    rollover: RolloverConfig = Field(default_factory=RolloverConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    timeframe_roles: TimeframeRoles = Field(default_factory=TimeframeRoles)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    setups: SetupsConfig = Field(default_factory=SetupsConfig)

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "EngineConfig":
        path = Path(path) if path else CONFIG_DIR / "default.yaml"
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}
        return cls(**raw)


# --------------------------------------------------------------------------- #
# Contract specs
# --------------------------------------------------------------------------- #
class ContractSpec(BaseModel):
    symbol: str
    name: str
    multiplier: float
    tick_size: float
    tick_value: float
    months: list[str]
    exchange: str = "CME"
    currency: str = "USD"
    is_micro: bool = False
    micro_of: Optional[str] = None

    @field_validator("multiplier", "tick_size", "tick_value")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    def points_to_dollars(self, points: float) -> float:
        return points * self.multiplier

    def ticks_from_points(self, points: float) -> float:
        return points / self.tick_size


class ContractsFile(BaseModel):
    version: int = 1
    verified: bool = False
    verification: dict = Field(default_factory=dict)
    month_codes: dict[str, int] = Field(default_factory=dict)
    contracts: dict[str, ContractSpec] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "ContractsFile":
        path = Path(path) if path else CONFIG_DIR / "contracts.yaml"
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}
        # inject symbol into each spec if omitted
        for sym, spec in (raw.get("contracts") or {}).items():
            spec.setdefault("symbol", sym)
        return cls(**raw)
