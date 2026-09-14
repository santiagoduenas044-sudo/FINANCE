"""Data quality validation for normalized OHLCV bars.

Detects: duplicate timestamps, non-chronological order, abnormal intra-session
gaps, and OHLCV integrity violations. Problems are reported (never silently
"fixed" in a way that could introduce look-ahead). A policy decides whether to
drop invalid rows and/or raise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from ..config.schema import ValidationConfig
from .models import CLOSE, HIGH, LOW, OHLCV, OPEN, SESSION, TRADING_DATE, VOLUME


class Severity(str, Enum):
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str
    count: int = 0
    sample: tuple = ()


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    n_rows: int = 0
    n_dropped: int = 0

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    def add(self, issue: Issue) -> None:
        if issue.count:
            self.issues.append(issue)

    def summary(self) -> str:
        if not self.issues:
            return f"OK ({self.n_rows} rows, no issues)"
        parts = [f"{i.severity.value} {i.code}: {i.message} (n={i.count})" for i in self.issues]
        return f"{self.n_rows} rows, {self.n_dropped} dropped | " + " | ".join(parts)


def validate(
    df: pd.DataFrame,
    cfg: ValidationConfig,
    base_interval: pd.Timedelta,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Validate a CLOSE-labeled OHLCV df (optionally session-annotated).

    Returns a (possibly filtered) df and a report. The input index is expected
    tz-aware. Does not mutate the input.
    """
    report = ValidationReport(n_rows=len(df))
    if df.empty:
        return df.copy(), report

    work = df.copy()

    # 1) duplicate timestamps
    dup_mask = work.index.duplicated(keep="first")
    n_dup = int(dup_mask.sum())
    if n_dup:
        report.add(Issue("duplicate_timestamps", Severity.ERROR,
                         "duplicate index timestamps", n_dup,
                         tuple(str(t) for t in work.index[dup_mask][:5])))

    # 2) chronological order (on the original, before any sort)
    unsorted = int((work.index.to_series().diff().dropna() < pd.Timedelta(0)).sum())
    if unsorted:
        report.add(Issue("out_of_order", Severity.ERROR,
                         "timestamps not in ascending order", unsorted))

    # 3) OHLCV integrity
    o, h, l, c, v = (work[OPEN], work[HIGH], work[LOW], work[CLOSE], work[VOLUME])
    nan_mask = work[OHLCV].isna().any(axis=1)
    n_nan = int(nan_mask.sum())
    if n_nan:
        report.add(Issue("nan_values", Severity.ERROR, "NaN in OHLCV", n_nan))

    bad_hl = (h < l)
    bad_high = (h < o) | (h < c)
    bad_low = (l > o) | (l > c)
    nonpos = (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
    neg_vol = (v < 0)
    integrity = (bad_hl | bad_high | bad_low | nonpos | neg_vol).fillna(True)
    n_integ = int(integrity.sum())
    if n_integ:
        report.add(Issue("ohlcv_integrity", Severity.ERROR,
                         "high<low, high/low vs open/close, non-positive price, or negative volume",
                         n_integ, tuple(str(t) for t in work.index[integrity][:5])))

    # 4) abnormal intra-session gaps (same session AND same trading date, so the
    #    daily maintenance halt and the RTH carve-out are not false-flagged)
    if SESSION in work.columns:
        ordered = work.sort_index()
        deltas = ordered.index.to_series().diff()
        same_session = ordered[SESSION].eq(ordered[SESSION].shift())
        if TRADING_DATE in ordered.columns:
            same_session &= ordered[TRADING_DATE].eq(ordered[TRADING_DATE].shift())
        gap_thresh = cfg.gap_factor * base_interval
        gap_mask = same_session & (deltas > gap_thresh)
        n_gap = int(gap_mask.sum())
        if n_gap:
            report.add(Issue("abnormal_gap", Severity.WARNING,
                             f"intra-session gap > {cfg.gap_factor}x base interval",
                             n_gap, tuple(str(t) for t in ordered.index[gap_mask][:5])))

    # apply policy
    invalid = pd.Series(False, index=work.index)
    invalid |= pd.Series(dup_mask, index=work.index)
    invalid |= nan_mask.fillna(True)
    invalid |= integrity
    if cfg.drop_invalid:
        keep = ~invalid.to_numpy()
        report.n_dropped = int((~keep).sum())
        work = work[keep]

    if cfg.raise_on_error and not report.ok:
        raise ValueError("validation failed: " + report.summary())

    return work, report
