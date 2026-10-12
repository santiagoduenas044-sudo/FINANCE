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
from .levels import in_the_way, levels_at
from .models import GhostDecision, MarketState, NoTradeReason
from .targets import apply_plan, plan_targets

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



# CME equity futures stop at 17:00 ET and reopen at 18:00 ET every day.
SESSION_REOPEN = "18:00"


def outside_entry_hours(hhmm: str, hours: list[str] | None) -> bool:
    """True when `hhmm` is outside the [start, end) ET window (wraps midnight)."""
    if not hours:
        return False
    start, end = hours[0], hours[1]
    inside = (start <= hhmm < end) if start < end else (hhmm >= start or hhmm < end)
    return not inside


def entry_window_closed(hhmm: str, last_entry_time: str | None) -> bool:
    """No new entries from `last_entry_time` until the daily reopen: e.g.
    16:15 -> 18:00 is closed, 18:00 onwards (the overnight session) is open
    again. Same as the old RTH-only rule inside RTH (15:30 -> 16:00)."""
    return bool(last_entry_time) and last_entry_time <= hhmm < SESSION_REOPEN


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
                 cooldown_check: Callable[[str], tuple[bool, dict] | tuple[bool, None]] | None = None,
                 duplicate_check: Callable[[str], bool] | None = None,
                 news_check: Callable | None = None,
                 ) -> tuple[MarketState, list[GhostDecision]]:
        """Return (market_state, decisions). `data_stale`, `has_active`,
        `cooldown_check` and `duplicate_check` are supplied by the runtime
        (feed status / paper tracker) — the brain stays pure and side-effect
        free. Signature is backward-compatible: any of the callables may be None."""
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
                                                  data_stale, conflicting, has_active,
                                                  cooldown_check, duplicate_check,
                                                  news_check))
        return ms, decisions

    # ------------------------------------------------------------------ #
    def _decide_signal(self, sig: Signal, ms: MarketState, exec_ta,
                       ctx: AnalysisContext, data_stale: bool, conflicting: bool,
                       has_active, cooldown_check=None,
                       duplicate_check=None, news_check=None) -> GhostDecision:
        cfg = self.cfg
        direction = sig.direction.value
        reasons: list[NoTradeReason] = []
        extra: dict = {}

        # Phase 15: restart-safe duplicate — same signal_id already processed.
        if duplicate_check is not None and duplicate_check(sig.signal_id):
            reasons.append(NoTradeReason.DUPLICATE)

        if data_stale:
            reasons.append(NoTradeReason.STALE_DATA)
        if ms.session not in cfg.valid_sessions:
            reasons.append(NoTradeReason.INVALID_SESSION)
        if outside_entry_hours(sig.timestamp.strftime("%H:%M"), cfg.entry_hours):
            reasons.append(NoTradeReason.INVALID_SESSION)
        if entry_window_closed(sig.timestamp.strftime("%H:%M"), cfg.last_entry_time):
            reasons.append(NoTradeReason.TOO_LATE)
        if conflicting:
            reasons.append(NoTradeReason.CONFLICTING_SETUPS)
        # high-impact news window (only when this strategy asks for it)
        if cfg.news_blackout and news_check is not None:
            ev = news_check(ctx.decision_time, cfg.news_blackout)
            if ev is not None:
                reasons.append(NoTradeReason.NEWS)
                extra["news"] = f"{ev.when:%H:%M} {ev.title}"
        # with the trend only (when this strategy asks for it)
        if cfg.trend_filter == "vwap":
            want = "ABOVE" if direction == "LONG" else "BELOW"
            if ms.vwap_side != want:
                reasons.append(NoTradeReason.AGAINST_TREND)
                extra["trend"] = f"precio {ms.vwap_side} del VWAP"
        # Phase 15: max_open_per_symbol=1 => any open trade on the symbol blocks.
        if has_active is not None and has_active(sig.instrument, direction, sig.setup):
            reasons.append(NoTradeReason.ACTIVE_TRADE)
        # Phase 15: cooldown after STOP/INVALIDATION.
        if cooldown_check is not None:
            in_cd, cd_info = cooldown_check(sig.instrument)
            if in_cd:
                reasons.append(NoTradeReason.COOLDOWN)
                if cd_info:
                    extra["cooldown"] = cd_info
        # Phase 15: invalid trade plan (defensive; setups should never emit this).
        if sig.stop == sig.entry or not sig.take_profits:
            reasons.append(NoTradeReason.INVALID_TRADE_PLAN)
        # Phase 15: stop-too-tight — |entry-stop| vs ATR14 on execution tf.
        stop_info = _stop_context(sig, exec_ta, cfg)
        if stop_info.get("too_tight"):
            reasons.append(NoTradeReason.STOP_TOO_TIGHT)
        if stop_info.get("too_wide"):
            reasons.append(NoTradeReason.STOP_TOO_WIDE)
        extra["stop_check"] = stop_info

        # Regime-adaptive targets. Applied BEFORE the R:R guard and scoring so
        # every check below judges the plan that would actually be traded.
        plan = plan_targets(sig, ms, cfg)
        sig = apply_plan(sig, plan)

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
        # "espacio libre": a support / resistance in the way to the target
        if cfg.clear_path and sig.take_profits:
            lv = in_the_way(direction, sig.entry, sig.take_profits[-1],
                            levels_at(ctx.execution.bars, ctx.decision_time), cfg.clear_path)
            if lv is not None:
                reasons.append(NoTradeReason.LEVEL_IN_THE_WAY)
                extra["level"] = f"{lv.price:.2f} {lv.label}"

        confluence = score_confluence(direction, ms, sig, exec_ta, cfg)
        plan_dict = ({"far_r": plan.far_r, "band": plan.band,
                      "basis": plan.basis,
                      "take_profits": list(plan.take_profits)}
                     if plan is not None else None)

        # Attach Phase 15 diagnostics (stop_check, cooldown info) into the
        # decision's market_context via detail — keeps GhostDecision immutable.
        detail_extra = ""
        if extra.get("stop_check"):
            sc = extra["stop_check"]
            detail_extra += (f" stop={sc.get('stop_distance')}pts atr={sc.get('atr')}"
                             f" ratio={sc.get('ratio')}")
        if extra.get("cooldown"):
            cd = extra["cooldown"]
            detail_extra += f" cooldown_until={cd.get('cooldown_until')}"
        if extra.get("news"):
            detail_extra += f" news={extra['news']}"
        if extra.get("level"):
            detail_extra += f" level={extra['level']}"

        if reasons:
            return GhostDecision(
                action="NO_TRADE", instrument=sig.instrument, setup=sig.setup,
                timestamp=sig.timestamp, market_state=ms, confluence=confluence,
                signal=sig, no_trade_reasons=_dedup(reasons),
                target_plan=plan_dict,
                detail=("hard guard(s) triggered" + detail_extra).strip())

        if not confluence.passed:
            return GhostDecision(
                action="NO_TRADE", instrument=sig.instrument, setup=sig.setup,
                timestamp=sig.timestamp, market_state=ms, confluence=confluence,
                signal=sig, no_trade_reasons=[NoTradeReason.INSUFFICIENT_CONFLUENCE],
                target_plan=plan_dict,
                detail=f"score {confluence.score} < {cfg.min_confluence_score}"
                       + detail_extra)

        return GhostDecision(
            action=direction, instrument=sig.instrument, setup=sig.setup,
            timestamp=sig.timestamp, market_state=ms, confluence=confluence,
            signal=sig, target_plan=plan_dict,
            detail=f"confluence {confluence.score}" + detail_extra)


def _stop_context(sig: Signal, exec_ta, cfg) -> dict:
    """Compute stop distance vs ATR14 on the execution timeframe. Returns
    {stop_distance, atr, ratio, threshold, too_tight, max_threshold, too_wide,
    max_points}. Never raises — a missing/degenerate ATR simply reports
    too_tight/too_wide=False (no false negative that would spuriously reject a
    trade). The absolute points ceiling does not depend on ATR, so it still
    applies when ATR is unavailable."""
    stop_dist = round(abs(sig.entry - sig.stop), 4)
    atr = None
    if exec_ta is not None and exec_ta.indicators is not None \
            and "atr_14" in exec_ta.indicators:
        s = exec_ta.indicators["atr_14"].dropna()
        if not s.empty and float(s.iloc[-1]) > 0:
            atr = round(float(s.iloc[-1]), 4)
    ratio = round(stop_dist / atr, 4) if atr else None
    threshold = cfg.min_stop_atr_mult
    too_tight = (threshold > 0 and atr is not None and stop_dist < threshold * atr)

    max_threshold = cfg.max_stop_atr_mult
    too_wide = (max_threshold > 0 and atr is not None
                and stop_dist > max_threshold * atr)
    max_points = (cfg.max_stop_points or {}).get(sig.instrument)
    if max_points is not None and max_points > 0 and stop_dist > max_points:
        too_wide = True

    return {"stop_distance": stop_dist, "atr": atr, "ratio": ratio,
            "threshold": threshold, "too_tight": too_tight,
            "max_threshold": max_threshold, "max_points": max_points,
            "too_wide": too_wide}


def _dedup(reasons: list[NoTradeReason]) -> list[NoTradeReason]:
    seen: set = set()
    out = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
