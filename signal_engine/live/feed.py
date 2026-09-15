"""LiveFeed (Phase 10): turns a LiveDataAdapter's incoming 1-minute bars into
the SAME multi-timeframe format the historical Data Engine produces — reusing
`SessionClassifier.annotate()` and `resample_ohlcv()` from Phase 3 EXACTLY,
no reimplementation.

No-lookahead: `get_tf_bars()` only ever contains bars that have already
CLOSED and been delivered by the adapter — by construction, "now" in live
mode is simply "the last bar we have", so `DataEngine.analysis_context()`
(unchanged from Phase 3) slices to it exactly as it does in backtest.

Reconnection: `poll()` catches `ConnectionError` from the adapter, retries a
bounded number of times with backoff, and — only after exhausting
attempts — reports DISCONNECTED. It never fabricates data and never silently
returns stale bars as if they were fresh.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from ..config.schema import EngineConfig, ValidationConfig
from ..data.models import SESSION, TRADING_DATE, tf_to_timedelta
from ..data.resample import resample_ohlcv
from ..data.sessions import SessionClassifier
from ..data.validation import validate
from .adapter import LiveDataAdapter
from .models import DataFeedStatus, FeedHealth, LiveBar

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveFeed:
    def __init__(
        self,
        adapter: LiveDataAdapter,
        symbol: str,
        config: EngineConfig,
        now_fn: Callable[[], datetime] = _utcnow,
        sleep_fn: Callable[[float], None] = lambda s: None,
    ):
        self.adapter = adapter
        self.symbol = symbol
        self.cfg = config
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.classifier = SessionClassifier(config.sessions)

        self._buffer = pd.DataFrame(
            {c: pd.Series(dtype="float64") for c in _OHLCV_COLS},
            index=pd.DatetimeIndex([], name="timestamp", tz=config.data.timezone),
        )
        self._last_update_at: datetime | None = None
        self._last_bar_ts: pd.Timestamp | None = None
        self._reconnect_attempts = 0
        self._connected = False
        self._last_error: str = ""
        self.last_validation_report = None
        # Live ingestion always drops clearly-broken incoming rows (NaN,
        # negative/non-positive prices, high<low, ...) to protect the running
        # buffer — reusing Phase 3's exact validation logic, just with a
        # live-appropriate (defensive) drop policy rather than the historical
        # Data Engine's default of reporting-only.
        self._ingest_validation_cfg = ValidationConfig(
            gap_factor=config.validation.gap_factor, drop_invalid=True, raise_on_error=False,
        )

    # ------------------------------------------------------------------ #
    # Polling / reconnection
    # ------------------------------------------------------------------ #
    def poll(self) -> list[LiveBar]:
        """Attempt to fetch new bars, reconnecting with bounded retries on
        failure. Returns the new bars (possibly empty); never raises."""
        if not self._connected:
            if not self._try_connect():
                return []

        try:
            new_bars = self.adapter.poll(self.symbol)
        except ConnectionError as exc:
            self._connected = False
            self._last_error = str(exc)
            if self._try_connect():
                try:
                    new_bars = self.adapter.poll(self.symbol)
                except ConnectionError as exc2:
                    self._last_error = str(exc2)
                    return []
            else:
                return []

        if new_bars:
            self._ingest(new_bars)
            self._reconnect_attempts = 0
            self._last_error = ""      # clear any stale error once data flows again
        return new_bars

    def _try_connect(self) -> bool:
        max_attempts = self.cfg.live.reconnect_max_attempts
        for attempt in range(max_attempts):
            try:
                self.adapter.connect()
                self._connected = True
                self._reconnect_attempts = 0
                return True
            except ConnectionError as exc:
                self._last_error = str(exc)
                self._reconnect_attempts += 1
                if attempt < max_attempts - 1:
                    self.sleep_fn(self.cfg.live.reconnect_backoff_seconds * (attempt + 1))
        self._connected = False
        return False

    def _ingest(self, bars: list[LiveBar]) -> None:
        rows = {b.timestamp: {"open": b.open, "high": b.high, "low": b.low,
                              "close": b.close, "volume": b.volume} for b in bars}
        new_df = pd.DataFrame.from_dict(rows, orient="index")
        new_df.index.name = "timestamp"
        if new_df.index.tz is None:
            new_df.index = new_df.index.tz_localize(self.cfg.data.timezone)

        base_interval = tf_to_timedelta(self.cfg.data.base_timeframe)
        new_df, report = validate(new_df, self._ingest_validation_cfg, base_interval)
        self.last_validation_report = report

        combined = pd.concat([self._buffer, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        # pd.concat with the initial empty (object-dtyped) buffer can leave
        # OHLCV as `object` dtype, which silently breaks downstream numeric
        # ops (e.g. groupby.cumsum in the VWAP indicator) — force float64,
        # exactly matching the historical Data Engine's normalized dtype.
        combined[_OHLCV_COLS] = combined[_OHLCV_COLS].astype("float64")

        cutoff = combined.index.max() - pd.Timedelta(days=self.cfg.live.buffer_max_days)
        self._buffer = combined[combined.index >= cutoff]

        self._last_update_at = self.now_fn()
        self._last_bar_ts = self._buffer.index.max()

    # ------------------------------------------------------------------ #
    # Output — same multi-timeframe format as the historical Data Engine
    # ------------------------------------------------------------------ #
    def get_tf_bars(self) -> dict[str, pd.DataFrame]:
        base_tf = self.cfg.data.base_timeframe
        if self._buffer.empty:
            return {}
        annotated = self.classifier.annotate(self._buffer, label_interval=tf_to_timedelta(base_tf))
        out = {base_tf: annotated}
        for tf in self.cfg.data.target_timeframes:
            if tf == base_tf:
                continue
            out[tf] = resample_ohlcv(annotated, tf, self.cfg.sessions, self.cfg.resample,
                                     self.cfg.data.timezone)
        return out

    # ------------------------------------------------------------------ #
    # Health / status — what the dashboard shows
    # ------------------------------------------------------------------ #
    def health(self) -> FeedHealth:
        if not self.adapter.is_connected or not self._connected:
            status = DataFeedStatus.DISCONNECTED
        elif self._last_update_at is None:
            status = DataFeedStatus.DISCONNECTED
        else:
            age = (self.now_fn() - self._last_update_at).total_seconds()
            status = (DataFeedStatus.LIVE if age <= self.cfg.live.stale_after_seconds
                     else DataFeedStatus.STALE)
        return FeedHealth(
            status=status, last_update_at=self._last_update_at,
            last_bar_timestamp=self._last_bar_ts,
            reconnect_attempts=self._reconnect_attempts, detail=self._last_error,
        )
