"""Daily OHLCV providers for the stock scanner.

- YahooDailyProvider: Yahoo Finance's public chart endpoint (no API key, the
  same one `yfinance` wraps). Prices are split/dividend-adjusted using the
  endpoint's `adjclose`. The HTTP transport is injectable so tests never touch
  the network.
- CsvDailyProvider: one `<TICKER>.csv` per symbol (date,open,high,low,close,volume).
- SyntheticDailyProvider: deterministic random walks with per-ticker drift, for
  offline demos and tests. NOT market data.

Every provider returns a DataFrame indexed by a tz-naive DatetimeIndex with
lowercase columns open/high/low/close/volume, oldest first.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import zlib
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range}"
_USER_AGENT = "Mozilla/5.0 (compatible; stock-scanner/1.0)"


class DataProvider(ABC):
    name: str = "base"

    @abstractmethod
    def history(self, ticker: str) -> pd.DataFrame:
        """Daily bars for `ticker`. Raises LookupError when unavailable."""

    def history_many(self, tickers: Iterable[str], workers: int = 8) -> dict[str, pd.DataFrame]:
        """Fetch several tickers; the ones that fail are logged and skipped."""
        tickers = list(dict.fromkeys(tickers))

        def _one(t: str) -> tuple[str, pd.DataFrame | None]:
            try:
                return t, self.history(t)
            except LookupError as exc:
                logger.warning("skip %s: %s", t, exc)
                return t, None

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return {t: df for t, df in pool.map(_one, tickers) if df is not None and not df.empty}


def _urllib_transport(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class YahooDailyProvider(DataProvider):
    name = "yahoo"

    def __init__(self, range_: str = "2y", timeout_seconds: float = 15.0,
                 transport: Callable[[str, float], bytes] | None = None):
        self._range = range_
        self._timeout = timeout_seconds
        self._transport = transport or _urllib_transport

    def history(self, ticker: str) -> pd.DataFrame:
        # Yahoo uses '-' for share classes (BRK.B -> BRK-B).
        url = _CHART_URL.format(ticker=ticker.replace(".", "-"), range=self._range)
        try:
            payload = json.loads(self._transport(url, self._timeout))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LookupError(f"yahoo request failed: {type(exc).__name__}: {exc}") from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise LookupError(f"yahoo API error: {chart['error']}")
        try:
            result = chart["result"][0]
            quote = result["indicators"]["quote"][0]
            ts = result["timestamp"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LookupError(f"yahoo response malformed: {type(exc).__name__}") from exc

        df = pd.DataFrame({c: quote.get(c) for c in COLUMNS},
                          index=pd.to_datetime(ts, unit="s").normalize())
        adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
        if adj is not None:
            # Scale OHLC by the adjustment factor so splits/dividends don't
            # show up as fake gaps in returns, ATR or moving averages.
            factor = pd.Series(adj, index=df.index, dtype="float64") / df["close"]
            for c in ("open", "high", "low", "close"):
                df[c] = df[c] * factor
        df = df.dropna(subset=["close"]).astype("float64")
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty:
            raise LookupError("yahoo returned no bars")
        return df


class CsvDailyProvider(DataProvider):
    name = "csv"

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    def history(self, ticker: str) -> pd.DataFrame:
        path = self._dir / f"{ticker.upper()}.csv"
        if not path.exists():
            raise LookupError(f"no CSV at {path}")
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        date_col = "date" if "date" in df.columns else df.columns[0]
        df.index = pd.to_datetime(df.pop(date_col)).dt.tz_localize(None)
        if "adj close" in df.columns:
            factor = df["adj close"] / df["close"]
            for c in ("open", "high", "low", "close"):
                df[c] = df[c] * factor
        missing = [c for c in COLUMNS if c not in df.columns]
        if missing:
            raise LookupError(f"{path.name} missing columns {missing}")
        return df[COLUMNS].astype("float64").sort_index()


class SyntheticDailyProvider(DataProvider):
    """Deterministic geometric random walks (seeded by ticker name) so the
    scanner, backtest and tests run fully offline. Each ticker gets its own
    drift and volatility, which gives the ranking something to find."""

    name = "synthetic"

    def __init__(self, days: int = 504, end: str | pd.Timestamp | None = None, seed: int = 7):
        self._days = days
        self._end = pd.Timestamp(end or pd.Timestamp.today()).normalize()
        self._seed = seed

    def history(self, ticker: str) -> pd.DataFrame:
        rng = np.random.default_rng(zlib.crc32(ticker.encode()) ^ self._seed)
        idx = pd.bdate_range(end=self._end, periods=self._days)
        drift = rng.normal(0.10, 0.25) / 252      # annual drift ~ N(10%, 25%)
        vol = rng.uniform(0.18, 0.55) / np.sqrt(252)
        # regime shift halfway through so momentum isn't trivially persistent
        drift2 = drift + rng.normal(0.0, 0.15) / 252
        mu = np.where(np.arange(self._days) < self._days // 2, drift, drift2)
        rets = rng.normal(mu, vol)
        close = rng.uniform(20, 400) * np.exp(np.cumsum(rets))
        open_ = close * np.exp(rng.normal(0, vol / 3, self._days))
        spread = np.abs(rng.normal(0, vol, self._days)) * close
        high = np.maximum(open_, close) + spread / 2
        low = np.minimum(open_, close) - spread / 2
        volume = rng.uniform(1e6, 2e7) * np.exp(rng.normal(0, 0.3, self._days))
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "volume": volume}, index=idx)


def make_provider(kind: str, csv_dir: str | None = None, range_: str = "2y") -> DataProvider:
    if kind == "yahoo":
        return YahooDailyProvider(range_=range_)
    if kind == "csv":
        if not csv_dir:
            raise ValueError("--csv-dir is required with the csv provider")
        return CsvDailyProvider(csv_dir)
    if kind == "synthetic":
        return SyntheticDailyProvider()
    raise ValueError(f"unknown provider {kind!r}")
