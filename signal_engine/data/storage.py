"""Parquet storage partitioned by symbol/contract/timeframe, with a sidecar
metadata JSON and a deterministic content hash for reproducibility.

The content hash depends ONLY on the data (and declared metadata), never on
timestamps like file mtime, so the same inputs + config reproduce the same
hash exactly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .models import OHLCV


def content_hash(df: pd.DataFrame) -> str:
    """Deterministic hash of a DataFrame's index + values."""
    h = pd.util.hash_pandas_object(df, index=True)
    # fold to a single stable hex digest
    import hashlib
    return hashlib.sha256(h.values.tobytes()).hexdigest()


def _part_dir(root: Path, symbol: str, contract: str, timeframe: str) -> Path:
    return root / f"symbol={symbol}" / f"contract={contract}" / f"timeframe={timeframe}"


def write_bars(
    df: pd.DataFrame,
    root: str | Path,
    symbol: str,
    contract: str,
    timeframe: str,
    metadata: dict | None = None,
) -> Path:
    root = Path(root)
    d = _part_dir(root, symbol, contract, timeframe)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "data.parquet"
    df.to_parquet(path, engine="pyarrow", index=True)
    meta = {
        "symbol": symbol,
        "contract": contract,
        "timeframe": timeframe,
        "rows": int(len(df)),
        "content_hash": content_hash(df),
        "columns": list(df.columns),
        "tz": str(df.index.tz) if df.index.tz is not None else None,
        **(metadata or {}),
    }
    with open(d / "meta.json", "w") as fh:
        json.dump(meta, fh, indent=2, default=str, sort_keys=True)
    return path


def read_bars(
    root: str | Path,
    symbol: str,
    contract: str,
    timeframe: str,
) -> pd.DataFrame:
    root = Path(root)
    path = _part_dir(root, symbol, contract, timeframe) / "data.parquet"
    df = pd.read_parquet(path, engine="pyarrow")
    return df


def read_meta(
    root: str | Path,
    symbol: str,
    contract: str,
    timeframe: str,
) -> dict:
    root = Path(root)
    with open(_part_dir(root, symbol, contract, timeframe) / "meta.json") as fh:
        return json.load(fh)
