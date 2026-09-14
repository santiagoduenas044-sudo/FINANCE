"""DataEngine: orchestrates the Phase-3 pipeline.

CSV -> raw ingestion -> normalization -> validation -> contract identification
-> session handling -> multi-timeframe resampling -> continuous/roll handling
-> Parquet storage -> AnalysisContext.

The engine depends only on the ``MarketDataProvider`` interface, so the source
(CSV/FirstRateData now, Databento later) is swappable without touching the core.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config.schema import EngineConfig
from .context import AnalysisContext, build_analysis_context
from .contracts import ContractInfo, ContractRegistry, parse_contract_code
from .ingestion import normalize
from .models import TRADING_DATE, tf_to_timedelta
from .providers.base import MarketDataProvider
from .resample import resample_ohlcv
from .rollover import RollEvent, build_continuous
from .sessions import SessionClassifier
from .validation import ValidationReport, validate


@dataclass
class LoadedContract:
    symbol: str
    contract: str
    info: ContractInfo | None
    base_tf: str
    bars: pd.DataFrame                        # normalized+validated+annotated (base tf)
    report: ValidationReport
    timeframes: dict[str, pd.DataFrame] = field(default_factory=dict)


class DataEngine:
    def __init__(self, config: EngineConfig | None = None,
                 registry: ContractRegistry | None = None):
        self.cfg = config or EngineConfig.load()
        self.registry = registry or ContractRegistry.load()
        self.classifier = SessionClassifier(self.cfg.sessions)

    # ---- single contract pipeline --------------------------------------- #
    def load_contract(
        self,
        provider: MarketDataProvider,
        symbol: str,
        contract: str,
        timeframe: str | None = None,
    ) -> LoadedContract:
        base_tf = timeframe or self.cfg.data.base_timeframe
        # contract identification is separate from validation; a malformed code
        # must not crash ingestion (info stays None).
        try:
            info = parse_contract_code(contract)
        except ValueError:
            info = None

        raw = provider.get_raw_bars(symbol, base_tf, contract=contract)
        # provider may declare its own convention; DataConfig default is used
        data_cfg = self.cfg.data
        if getattr(provider, "source_timestamp_convention", None):
            data_cfg = data_cfg.model_copy(
                update={"input_timestamp_convention": provider.source_timestamp_convention}
            )

        norm = normalize(raw, data_cfg, base_tf)
        annotated = self.classifier.annotate(norm, label_interval=tf_to_timedelta(base_tf))
        validated, report = validate(
            annotated, self.cfg.validation, tf_to_timedelta(base_tf)
        )
        return LoadedContract(symbol, contract, info, base_tf, validated, report)

    def build_timeframes(self, loaded: LoadedContract) -> dict[str, pd.DataFrame]:
        """Generate the configured target timeframes from the base bars."""
        out: dict[str, pd.DataFrame] = {loaded.base_tf: loaded.bars}
        for tf in self.cfg.data.target_timeframes:
            if tf == loaded.base_tf:
                continue
            out[tf] = resample_ohlcv(
                loaded.bars, tf, self.cfg.sessions, self.cfg.resample,
                self.cfg.data.timezone,
            )
        loaded.timeframes = out
        return out

    # ---- continuous series ---------------------------------------------- #
    def build_continuous(
        self,
        provider: MarketDataProvider,
        symbol: str,
        contracts: list[str],
        timeframe: str | None = None,
    ) -> tuple[pd.DataFrame, list[RollEvent]]:
        base_tf = timeframe or self.cfg.data.base_timeframe
        contract_dfs: dict[str, pd.DataFrame] = {}
        for c in contracts:
            lc = self.load_contract(provider, symbol, c, base_tf)
            contract_dfs[c] = lc.bars
        return build_continuous(contract_dfs, self.cfg.rollover)

    # ---- storage --------------------------------------------------------- #
    def store_timeframes(self, loaded: LoadedContract, extra_meta: dict | None = None) -> None:
        from . import storage
        meta = {
            "source_convention": self.cfg.data.input_timestamp_convention,
            "config_version": self.cfg.version,
            **(extra_meta or {}),
        }
        for tf, df in loaded.timeframes.items():
            storage.write_bars(
                df, self.cfg.storage.root, loaded.symbol, loaded.contract, tf,
                metadata=meta,
            )

    # ---- as-of context --------------------------------------------------- #
    def analysis_context(
        self,
        symbol: str,
        decision_time: pd.Timestamp,
        tf_bars: dict[str, pd.DataFrame],
    ) -> AnalysisContext:
        if decision_time.tzinfo is None:
            decision_time = decision_time.tz_localize(self.cfg.data.timezone)
        else:
            decision_time = decision_time.tz_convert(self.cfg.data.timezone)
        session = self.classifier.classify(decision_time)
        return build_analysis_context(
            symbol, decision_time, tf_bars, self.cfg.timeframe_roles, session,
        )
