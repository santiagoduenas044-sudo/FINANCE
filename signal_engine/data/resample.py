"""Multi-timeframe resampling WITHOUT look-ahead.

Higher timeframe bars are built per (trading_date, session) group so bins never
span the RTH/ETH boundary or the maintenance halt. Bins are CLOSE-labeled
(label='right', closed='right'): a bar's timestamp is the instant it closes, so
an as-of filter (index <= decision_time) can never see a still-forming bar.
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import ResampleConfig, SessionsConfig
from .models import (
    CLOSE, CLOSED, HIGH, LOW, OHLCV, OPEN, SESSION, TRADING_DATE, VOLUME,
    tf_to_offset, tf_to_timedelta,
)
from .sessions import SessionClassifier

_AGG = {OPEN: "first", HIGH: "max", LOW: "min", CLOSE: "last", VOLUME: "sum"}


def resample_ohlcv(
    df_annotated: pd.DataFrame,
    timeframe: str,
    sessions_cfg: SessionsConfig,
    resample_cfg: ResampleConfig,
    tz: str,
    reannotate: bool = True,
) -> pd.DataFrame:
    """Resample an annotated (SESSION, TRADING_DATE) CLOSE-labeled df to a higher
    timeframe. Returns a CLOSE-labeled OHLCV frame (optionally re-annotated)."""
    if SESSION not in df_annotated.columns or TRADING_DATE not in df_annotated.columns:
        raise ValueError("resample requires session-annotated input")

    offset = tf_to_offset(timeframe)
    classifier = SessionClassifier(sessions_cfg)

    # exclude CLOSED bars (not tradeable, not resampled)
    df = df_annotated[df_annotated[SESSION] != CLOSED]
    if df.empty:
        from .models import empty_ohlcv
        return empty_ohlcv(tz)

    pieces = []
    # group preserves determinism via sorted keys
    for (tdate, session), grp in df.groupby([TRADING_DATE, SESSION], sort=True):
        grp = grp.sort_index()
        if resample_cfg.origin == "session_open":
            origin = classifier.session_open_dt(tdate, session, tz)
        else:
            origin = "epoch"
        res = grp[OHLCV].resample(
            offset, label="right", closed="right", origin=origin
        ).agg(_AGG).dropna(how="all")
        if not res.empty:
            res[SESSION] = session
            res[TRADING_DATE] = tdate
            pieces.append(res)

    if not pieces:
        from .models import empty_ohlcv
        return empty_ohlcv(tz)

    out = pd.concat(pieces).sort_index()
    # volume dtype
    out[VOLUME] = out[VOLUME].fillna(0.0)
    if not reannotate:
        out = out[OHLCV]
    return out
