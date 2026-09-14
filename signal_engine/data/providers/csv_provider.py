"""Generic CSV provider.

Directory layout (configurable):
    <root>/<SYMBOL>/<CONTRACT>_<TIMEFRAME>.csv
e.g. data_samples/ES/ESU25_1m.csv

CSV columns (case-insensitive): a datetime column + open,high,low,close,volume.
The datetime column name is configurable (default "datetime").
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import MarketDataProvider


class CsvProvider(MarketDataProvider):
    def __init__(
        self,
        root: str | Path,
        datetime_col: str = "datetime",
        source_timestamp_convention: str = "open",
    ):
        self.root = Path(root)
        self.datetime_col = datetime_col
        self.source_timestamp_convention = source_timestamp_convention

    def _path(self, symbol: str, contract: str, timeframe: str) -> Path:
        return self.root / symbol / f"{contract}_{timeframe}.csv"

    def list_contracts(self, symbol: str) -> list[str]:
        d = self.root / symbol
        if not d.exists():
            return []
        codes = {p.stem.split("_")[0] for p in d.glob("*.csv")}
        return sorted(codes)

    def get_raw_bars(self, symbol: str, timeframe: str, contract: str | None = None) -> pd.DataFrame:
        if contract is None:
            raise ValueError("CsvProvider requires an explicit contract")
        path = self._path(symbol, contract, timeframe)
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(path)
        df.columns = [c.lower() for c in df.columns]
        dt_col = self.datetime_col.lower()
        df[dt_col] = pd.to_datetime(df[dt_col])
        df = df.set_index(dt_col)
        df.index.name = "raw_ts"
        return df[["open", "high", "low", "close", "volume"]]
