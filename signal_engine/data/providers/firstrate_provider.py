"""FirstRateData CSV provider.

FirstRateData intraday files are typically:
    <root>/<CONTRACT>_<interval>.txt|csv
with rows: ``YYYY-MM-DD HH:MM:SS,open,high,low,close,volume`` (no header),
timestamps in US/Eastern at the START of the bar. This adapter normalizes the
naming/format into raw OHLCV; tz-localization + close-labeling happen in the
DataEngine ingestion step, so this stays a thin, swappable adapter.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import MarketDataProvider

# FirstRateData interval tokens -> our timeframe strings
_INTERVAL_MAP = {
    "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
    "1hour": "1H", "1day": "1D",
}
_REVERSE = {v: k for k, v in _INTERVAL_MAP.items()}


class FirstRateProvider(MarketDataProvider):
    source_timestamp_convention = "open"

    def __init__(self, root: str | Path, symbol_map: dict[str, str] | None = None):
        self.root = Path(root)
        # optional map from our symbol ("ES") to FirstRate file prefix
        self.symbol_map = symbol_map or {}

    def _prefix(self, symbol: str) -> str:
        return self.symbol_map.get(symbol, symbol)

    def _path(self, contract: str, timeframe: str) -> Path:
        token = _REVERSE.get(timeframe, timeframe)
        for ext in (".csv", ".txt"):
            p = self.root / f"{contract}_{token}{ext}"
            if p.exists():
                return p
        return self.root / f"{contract}_{token}.csv"

    def list_contracts(self, symbol: str) -> list[str]:
        prefix = self._prefix(symbol)
        codes = set()
        for p in self.root.glob(f"{prefix}*"):
            stem = p.stem
            codes.add(stem.split("_")[0])
        return sorted(codes)

    def get_raw_bars(self, symbol: str, timeframe: str, contract: str | None = None) -> pd.DataFrame:
        if contract is None:
            raise ValueError("FirstRateProvider requires an explicit contract")
        path = self._path(contract, timeframe)
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(
            path, header=None,
            names=["datetime", "open", "high", "low", "close", "volume"],
        )
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")
        df.index.name = "raw_ts"
        return df[["open", "high", "low", "close", "volume"]]
