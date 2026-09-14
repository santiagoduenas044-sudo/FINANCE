"""Reproducible, volume-based rollover and continuous series construction.

Given the individual contracts' bars, we:
  1. order contracts by expiration,
  2. pick a roll date per consecutive pair = first trade date (within a lookback
     window before the front's expiration) where the back contract's daily
     volume exceeds the front's by ``volume_ratio``,
  3. build the continuous series: ``raw`` (actual traded prices, with jumps at
     rolls) and ``ratio`` (back-adjusted so returns are continuous).

Determinism: same inputs + config => same roll dates => same continuous series.
Price levels for ICT concepts must be computed on the ACTIVE contract's raw
bars; the ratio series is for aggregate performance stats only.
See docs/MARKET_DATA.md §6.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..config.schema import RolloverConfig
from .contracts import ContractInfo, parse_contract_code
from .models import CLOSE, HIGH, LOW, OHLC, OPEN, TRADING_DATE, VOLUME


@dataclass(frozen=True)
class RollEvent:
    symbol: str
    from_contract: str
    to_contract: str
    roll_date: date
    ratio: float          # back_close / front_close at the reference day


@dataclass(frozen=True)
class ActiveWindow:
    contract_code: str
    start: date           # inclusive (trade date)
    end: date | None      # inclusive; None = open-ended (last contract)


def _daily(df: pd.DataFrame) -> pd.DataFrame:
    """Daily (per trade date) last close and summed volume."""
    g = df.groupby(TRADING_DATE)
    return pd.DataFrame({
        "close": g[CLOSE].last(),
        "volume": g[VOLUME].sum(),
    })


def compute_roll_schedule(
    contract_dfs: dict[str, pd.DataFrame],
    cfg: RolloverConfig,
) -> tuple[list[RollEvent], list[ActiveWindow]]:
    infos = sorted(
        (parse_contract_code(c) for c in contract_dfs),
        key=lambda ci: ci.sort_key,
    )
    if not infos:
        return [], []

    dailies = {ci.contract_code: _daily(contract_dfs[ci.contract_code]) for ci in infos}

    events: list[RollEvent] = []
    for front, back in zip(infos, infos[1:]):
        fd, bd = dailies[front.contract_code], dailies[back.contract_code]
        common = fd.index.intersection(bd.index)
        # restrict to lookback window before front expiration
        window = [d for d in common
                  if (front.expiration - d).days <= cfg.lookback_days and d <= front.expiration]
        window = sorted(window)
        roll_date = None
        for d in window:
            if bd.loc[d, "volume"] > fd.loc[d, "volume"] * cfg.volume_ratio:
                roll_date = d
                break
        if roll_date is None:
            # fallback: roll on the last common date on/before expiration
            roll_date = window[-1] if window else (common.max() if len(common) else front.expiration)
        # reference day = latest common date strictly before roll_date (else roll_date)
        prior = [d for d in common if d < roll_date]
        ref = max(prior) if prior else roll_date
        ratio = float(bd.loc[ref, "close"] / fd.loc[ref, "close"]) if ref in fd.index and ref in bd.index else 1.0
        events.append(RollEvent(front.symbol, front.contract_code, back.contract_code, roll_date, ratio))

    # active windows
    windows: list[ActiveWindow] = []
    starts = {infos[0].contract_code: min(dailies[infos[0].contract_code].index)}
    for i, ci in enumerate(infos):
        start = starts.get(ci.contract_code, min(dailies[ci.contract_code].index))
        if i < len(events):
            end = events[i].roll_date - pd.Timedelta(days=1)
            end = end.date() if isinstance(end, pd.Timestamp) else end
            windows.append(ActiveWindow(ci.contract_code, start, end))
            starts[infos[i + 1].contract_code] = events[i].roll_date
        else:
            windows.append(ActiveWindow(ci.contract_code, start, None))
    return events, windows


def build_continuous(
    contract_dfs: dict[str, pd.DataFrame],
    cfg: RolloverConfig,
) -> tuple[pd.DataFrame, list[RollEvent]]:
    """Build the continuous series per ``cfg.continuous_adjustment``."""
    events, windows = compute_roll_schedule(contract_dfs, cfg)
    if not windows:
        raise ValueError("no contracts provided for continuous series")

    # cumulative ratio factor per contract (newest = 1.0)
    codes_in_order = [w.contract_code for w in windows]
    ratio_by_index = [e.ratio for e in events]  # roll i transitions contract i -> i+1
    factors: dict[str, float] = {}
    for k, code in enumerate(codes_in_order):
        f = 1.0
        for j in range(k, len(ratio_by_index)):
            f *= ratio_by_index[j]
        factors[code] = f

    pieces = []
    for w in windows:
        df = contract_dfs[w.contract_code]
        mask = df[TRADING_DATE] >= w.start
        if w.end is not None:
            mask &= df[TRADING_DATE] <= w.end
        piece = df[mask].copy()
        piece["contract"] = w.contract_code
        if cfg.continuous_adjustment == "ratio":
            factor = factors[w.contract_code]
            for col in OHLC:
                piece[col] = piece[col] * factor
        pieces.append(piece)

    continuous = pd.concat(pieces).sort_index()
    return continuous, events
