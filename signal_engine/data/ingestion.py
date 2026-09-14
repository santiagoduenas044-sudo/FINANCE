"""Raw ingestion + normalization to the canonical bar schema.

Turns a provider's raw OHLCV frame into the canonical form:
  - tz-aware DatetimeIndex named ``timestamp`` in the canonical tz
  - CLOSE-labeled (converted from the source's open/close convention)
  - float64 OHLCV columns, sorted ascending
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import DataConfig
from .models import CLOSE, HIGH, LOW, OHLCV, OPEN, TIMESTAMP, VOLUME, tf_to_timedelta


def normalize(
    raw: pd.DataFrame,
    cfg: DataConfig,
    source_timeframe: str,
) -> pd.DataFrame:
    """Normalize a raw frame with a datetime index (naive or tz-aware) and
    OHLCV columns into the canonical, CLOSE-labeled representation.
    """
    df = raw.copy()

    # ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("ingestion expects a DatetimeIndex")

    # tz localize/convert to canonical tz
    if df.index.tz is None:
        df.index = df.index.tz_localize(
            cfg.timezone,
            ambiguous=cfg.dst_ambiguous,
            nonexistent=cfg.dst_nonexistent,
        )
    else:
        df.index = df.index.tz_convert(cfg.timezone)
    df.index.name = TIMESTAMP

    # convert open-labeled timestamps to close-labeled
    if cfg.input_timestamp_convention == "open":
        df.index = df.index + tf_to_timedelta(source_timeframe)

    # keep only canonical columns, coerce dtypes
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"raw frame missing columns: {missing}")
    df = df[OHLCV].astype("float64")

    # stable sort by time (do NOT drop duplicates here; validation reports them)
    df = df.sort_index(kind="mergesort")
    return df
