"""SimulatedLiveAdapter (Phase 10): replays a historical 1-minute OHLCV frame
as if it were arriving live, one (or a few) bars per `poll()`.

This is the MVP source — analogous to `CsvProvider` in the historical Data
Engine (Phase 3). It implements the exact same `LiveDataAdapter` interface any
real vendor (Databento, Interactive Brokers, ...) would, so swapping the
source later requires no changes to `LiveFeed`, `BotRuntime`, or anything
upstream. Deterministic fault injection (`simulate_disconnect`/
`simulate_reconnect`) lets tests exercise reconnection without real time or
real network.
"""
from __future__ import annotations

import pandas as pd

from ..data.models import CLOSE, HIGH, LOW, OPEN, VOLUME
from .adapter import LiveDataAdapter
from .models import LiveBar


class SimulatedLiveAdapter(LiveDataAdapter):
    name = "simulated"

    def __init__(self, bars: pd.DataFrame, symbol: str, bars_per_poll: int = 1):
        """`bars` must already be normalized (tz-aware DatetimeIndex, CLOSE-
        labeled, OHLCV columns) — same contract as the historical pipeline."""
        self._bars = bars
        self._symbol = symbol
        self._bars_per_poll = max(1, bars_per_poll)
        self._cursor = 0
        self._connected = False
        self._force_disconnected = False

    def connect(self) -> None:
        if self._force_disconnected:
            raise ConnectionError("simulated adapter: connection refused (forced)")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def poll(self, symbol: str) -> list[LiveBar]:
        if self._force_disconnected:
            self._connected = False
            raise ConnectionError("simulated adapter: connection dropped")
        if not self._connected:
            raise ConnectionError("simulated adapter: not connected")
        if symbol != self._symbol or self._cursor >= len(self._bars):
            return []

        end = min(self._cursor + self._bars_per_poll, len(self._bars))
        rows = self._bars.iloc[self._cursor:end]
        self._cursor = end
        return [
            LiveBar(symbol=symbol, timestamp=ts, open=float(row[OPEN]),
                   high=float(row[HIGH]), low=float(row[LOW]),
                   close=float(row[CLOSE]), volume=float(row[VOLUME]))
            for ts, row in rows.iterrows()
        ]

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._bars)

    # ---- deterministic fault injection (tests / demos only) --------------- #
    def simulate_disconnect(self) -> None:
        self._force_disconnected = True
        self._connected = False

    def simulate_reconnect(self) -> None:
        self._force_disconnected = False
