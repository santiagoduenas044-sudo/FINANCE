"""GhostBrain (Phase 14) — the decision layer that makes GHOST interpret context
before deciding, instead of "setup detected == trade".

Flow per decision bar:

    build MarketState (regime, MTF, structure, liquidity, key levels)
      -> for each base result from the (unchanged) Signal Engine:
           base NoTrade  -> GHOST NO_TRADE (reasons carried through)
           base Signal   -> hard NO_TRADE guards (session, volatility, R:R,
                            HTF conflict, against-structure, conflicting setups,
                            stale data, duplicate active trade)
                         -> confluence score; below threshold -> NO_TRADE
                         -> otherwise LONG/SHORT with an explainable score

GHOST NEVER modifies the Data/Analysis/Setup/Signal/Risk engines — it consumes
their outputs. It never fabricates data: if a fact isn't derivable, the relevant
contribution is simply absent and the decision is more conservative.
"""
from __future__ import annotations

from typing import Callable

from ..config.schema import GhostConfig
from ..data.context import AnalysisContext
from ..features.analyzer import MarketAnalysis
from ..signals.models import NoTrade, Signal
from .confluence import score_confluence
from .context import build_market_state
from .models import GhostDecision, MarketState, NoTradeReason

# Best-effort mapping of base-engine NoTrade reason text -> a GHOST reason.
_BASE_REASON_MAP = {
    "risk": NoTradeReason.INSUFFICIENT_RR,
    "reward": NoTradeReason.INSUFFICIENT_RR,
    "expired": NoTradeReason.SETUP_INCOMPLETE,
    "conflict": NoTradeReason.CONFLICTING_SETUPS,
    "entry": NoTradeReason.SETUP_INCOMPLETE,
    "status": NoTradeReason.SETUP_INCOMPLETE,
    "side": NoTradeReason.SETUP_INCOMPLETE,
}


def _map_base_reason(text: str) -> NoTradeReason:
    low = text.lower()
    for key, reason in _BASE_REASON_MAP.items():
        if key in low:
            return reason
    return NoTradeReason.SETUP_INCOMPLETE


class GhostBrain:
    def __init__(self, cfg: GhostConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def market_state(self, ctx: AnalysisContext, analysis: MarketAnalysis) -> MarketState:
        return build_market_state(ctx, analysis, self.cfg)

    def evaluate(self, ctx: AnalysisContext, analysis: MarketAnalysis,
                 base_results: list[Signal | NoTrade], *,
                 data_stale: bool = False,
                 has_active: Callable[[str, str, str], bool] | None = None,
                 ) -> tuple[MarketState, list[GhostDecision]]:
        """Return (market_state, decisions). `data_stale` and `has_active` are
        supplied by the runtime (feed status / paper tracker) — the brain stays
        pure and side-effect free."""
        ms = self.market_state(ctx, analysis)
        exec_ta = analysis.by_timeframe.get(ctx.execution.timeframe)

        # conflicting setups: opposite-direction Signals on the same bar
        dirs = {s.direction.value for s in base_results if isinstance(s, Signal)}
        conflicting = {"LONG", "SHORT"} <= dirs

        decisions: list[GhostDecision] = []
        for res in base_results:
            if isinstance(res, NoTrade):
                reasons = [_map_base_reason(r) for r in (res.reasons or [])] \
                    or [NoTradeReason.SETUP_INCOMPLETE]
                decisions.append(GhostDecision(
                    action="NO_TRADE", instrument=res.instrument, setup=res.setup,
                    timestamp=res.timestamp, market_state=ms,
                    no_trade_reasons=_dedup(reasons),
                    detail="base engine returned NoTrade"))
                continue

            decisions.append(self._decide_signal(res, ms, exec_ta, ctx,
                                                  data_stale, conflicting, has_active))
        return ms, decisions

    # ------------------------------------------------------------------ #
    def _decide_signal(self, sig: Signal, ms: MarketState, exec_ta,
                       ctx: AnalysisContext, data_stale: bool, conflicting: bool,
                       has_active) -> GhostDecision:
        cfg = self.cfg
        direction = sig.direction.value
        reasons: list[NoTradeReason] = []

        if data_stale:
            reasons.append(NoTradeReason.STALE_DATA)
        if ms.session not in cfg.valid_sessions:
            reasons.append(NoTradeReason.INVALID_SESSION)
        if conflicting:
            reasons.append(NoTradeReason.CONFLICTING_SETUPS)
        if has_active is not None and has_active(sig.instrument, direction, sig.setup):
            reasons.append(NoTradeReason.DUPLICATE_ACTIVE_TRADE)
        if ms.regime.atr_percentile is not None and \
                ms.regime.atr_percentile > cfg.max_atr_percentile:
            reasons.append(NoTradeReason.EXCESSIVE_VOLATILITY)
        rr = min(sig.risk_reward) if sig.risk_reward else None
        if rr is not None and rr < cfg.min_rr:
            reasons.append(NoTradeReason.TARGET_TOO_CLOSE)
            reasons.append(NoTradeReason.INSUFFICIENT_RR)
        if cfg.block_htf_conflict and ms.mtf.conflicts_with(direction):
            reasons.append(NoTradeReason.CONFLICTING_TIMEFRAME)
        if cfg.block_against_structure and ms.structure.opposes(direction):
            reasons.append(NoTradeReason.AGAINST_STRUCTURE)

        confluence = score_confluence(direction, ms, sig, exec_ta, cfg)

        if reasons:
            return GhostDecision(
                action="NO_TRADE", instrument=sig.instrument, setup=sig.setup,
                timestamp=sig.timestamp, market_state=ms, confluence=confluence,
                signal=sig, no_trade_reasons=_dedup(reasons),
                detail="hard guard(s) triggered")

        if not confluence.passed:
            return GhostDecision(
                action="NO_TRADE", instrument=sig.instrument, setup=sig.setup,
                timestamp=sig.timestamp, market_state=ms, confluence=confluence,
                signal=sig, no_trade_reasons=[NoTradeReason.INSUFFICIENT_CONFLUENCE],
                detail=f"score {confluence.score} < {cfg.min_confluence_score}")

        return GhostDecision(
            action=direction, instrument=sig.instrument, setup=sig.setup,
            timestamp=sig.timestamp, market_state=ms, confluence=confluence,
            signal=sig, detail=f"confluence {confluence.score}")


def _dedup(reasons: list[NoTradeReason]) -> list[NoTradeReason]:
    seen: set = set()
    out = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
