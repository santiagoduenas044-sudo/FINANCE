"""Provider abstraction. The core depends ONLY on this interface, never on a
concrete provider. Adding Databento later = a new class implementing this ABC,
with zero changes to the rest of the system.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class MarketDataProvider(ABC):
    """Returns RAW OHLCV frames (a DatetimeIndex + open/high/low/close/volume).
    Normalization/validation is done by the DataEngine, not the provider, so all
    providers are interchangeable."""

    #: the source's native bar labeling ("open" or "close") and timeframe are
    #: reported so ingestion can normalize consistently.
    source_timestamp_convention: str = "open"

    @abstractmethod
    def get_raw_bars(self, symbol: str, timeframe: str, contract: str | None = None) -> pd.DataFrame:
        ...

    @abstractmethod
    def list_contracts(self, symbol: str) -> list[str]:
        ...
