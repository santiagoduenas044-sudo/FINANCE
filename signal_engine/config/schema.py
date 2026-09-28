"""Typed configuration models (pydantic v2) for the Data Engine.

Config is loaded from YAML and validated here. Nothing in the strategy/risk
logic should hardcode values that belong in configuration.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(__file__).resolve().parent
STRATEGIES_DIR = CONFIG_DIR / "strategies"
DEFAULT_STRATEGY = "zones"


# --------------------------------------------------------------------------- #
# Data Engine config
# --------------------------------------------------------------------------- #
class DataConfig(BaseModel):
    timezone: str = "America/New_York"
    base_timeframe: str = "1m"
    target_timeframes: list[str] = Field(default_factory=lambda: ["5m", "15m", "1H"])
    input_timestamp_convention: Literal["open", "close"] = "open"
    dst_ambiguous: str = "infer"
    dst_nonexistent: str = "shift_forward"


class ValidationConfig(BaseModel):
    gap_factor: float = 3.0
    drop_invalid: bool = False
    raise_on_error: bool = False


class RthConfig(BaseModel):
    start: str = "09:30"
    end: str = "16:00"


class MaintenanceBreak(BaseModel):
    start: str = "17:00"
    end: str = "18:00"


class EthConfig(BaseModel):
    enabled: bool = True
    start: str = "18:00"
    end: str = "17:00"
    maintenance_break: MaintenanceBreak = Field(default_factory=MaintenanceBreak)


class SessionsConfig(BaseModel):
    rth: RthConfig = Field(default_factory=RthConfig)
    eth: EthConfig = Field(default_factory=EthConfig)
    trade_date_rollover: str = "17:00"


class ResampleConfig(BaseModel):
    origin: Literal["session_open", "epoch"] = "session_open"


class RolloverConfig(BaseModel):
    method: Literal["volume"] = "volume"
    volume_ratio: float = 1.0
    lookback_days: int = 15
    continuous_adjustment: Literal["raw", "ratio"] = "ratio"


class StorageConfig(BaseModel):
    root: str = "data_store"


class TimeframeRoles(BaseModel):
    context: str = "1H"
    structure: str = "15m"
    execution: str = "5m"


class AnalysisConfig(BaseModel):
    """Market Analysis (Phase 4) backend selection."""
    structure_backend: Literal["native", "smc"] = "native"
    native_n_left: int = 3
    native_n_right: int = 3
    smc_swing_length: int = 8
    with_order_blocks: bool = True
    # Analyse only the most recent N bars of each timeframe. None = the whole
    # buffer. A 1m execution timeframe holds ~7000 bars; the structure
    # scans are superlinear, so capping keeps a cycle well under a second.
    max_bars: Optional[int] = None


class LiquiditySweepParams(BaseModel):
    enabled: bool = True
    timeframe: str = "5m"
    stop_buffer_ticks: float = 2.0
    rr_targets: list[float] = Field(default_factory=lambda: [1.0, 2.0])
    expiration_bars: int = 6
    use_opposing_liquidity_target: bool = True


class VwapReclaimParams(BaseModel):
    enabled: bool = True
    timeframe: str = "5m"
    lookback_bars: int = 12
    stop_buffer_ticks: float = 2.0
    rr_targets: list[float] = Field(default_factory=lambda: [1.0, 2.0])
    expiration_bars: int = 6


class OrbParams(BaseModel):
    enabled: bool = True
    timeframe: str = "5m"
    opening_range_minutes: int = 30
    session: str = "RTH"
    rr_targets: list[float] = Field(default_factory=lambda: [1.0, 2.0])  # R multiples
    expiration_bars: int = 12


class ZoneRetraceParams(BaseModel):
    """First retrace into a fresh fair-value-gap zone, confirmed by a reaction
    candle (setups/zone_retrace.py). All distances are index points."""
    enabled: bool = False
    timeframe: str = "1m"             # confirmation / execution timeframe
    zone_timeframe: str = "5m"        # where the zones (FVGs) are found
    zone_lookback_bars: int = 36      # zone_timeframe bars searched for zones
    min_zone_points: float = 3.0      # thinner gaps are noise
    max_zone_points: float = 40.0     # wider gaps put the midpoint stop inside the zone
    # Stop beyond the origin of the move that left the gap (the candle before
    # the displacement), measured from the zone midpoint and clamped to
    # [min_stop_points, max_stop_points]: TradingMind's longs show exactly 20,
    # its direct short 35.
    min_stop_points: float = 20.0
    max_stop_points: float = 35.0
    stop_buffer_points: float = 1.0
    max_risk_points: float = 50.0     # from the actual fill; price ran too far: skip
    arm_distance_points: float = 30.0  # heads-up once price is this close to the zone
    confirm_bars: int = 5             # bars after the first touch to show a reaction
    tp1_r: float = 1.0                # first target (stop goes to breakeven there)
    tp1_max_r: float = 2.0            # ...or an untaken swing up to this R
    min_target_r: float = 2.0         # structural final target must pay at least this
    max_target_r: float = 5.0         # ...and no more than this
    fallback_target_r: float = 3.0    # no structure in range -> fixed R
    # Fixed risk:reward instead of the structural target: e.g. 2.0 = "2:1",
    # the final target sits 2x the stop distance from the midpoint.
    fixed_target_r: Optional[float] = None
    # One target only (no TP1 / partial): the whole position rides to the
    # final target or the stop.
    single_target: bool = False
    expiration_bars: int = 5
    # The point values above are NQ points. Other symbols scale them: ES moves
    # roughly a quarter of NQ's points for the same % move.
    points_scale: dict[str, float] = Field(
        default_factory=lambda: {"ES": 0.25, "MES": 0.25})

    def for_symbol(self, symbol: str) -> "ZoneRetraceParams":
        k = self.points_scale.get(symbol, 1.0)
        if k == 1.0:
            return self
        return self.model_copy(update={
            f: getattr(self, f) * k for f in (
                "min_zone_points", "max_zone_points", "min_stop_points",
                "max_stop_points", "stop_buffer_points", "max_risk_points",
                "arm_distance_points")})


class SetupsConfig(BaseModel):
    liquidity_sweep: LiquiditySweepParams = Field(default_factory=LiquiditySweepParams)
    vwap_reclaim: VwapReclaimParams = Field(default_factory=VwapReclaimParams)
    orb: OrbParams = Field(default_factory=OrbParams)
    zone_retrace: ZoneRetraceParams = Field(default_factory=ZoneRetraceParams)


class SignalConfig(BaseModel):
    """Signal Engine (Phase 6): SetupCandidate -> Signal | NoTrade.

    Reasonable, un-optimized defaults — no parameter sweeps here."""
    min_risk_reward: float = 1.0          # nearest-TP R:R must be >= this, else NO_TRADE
    deduplication_enabled: bool = True    # same candidate never converted twice
    validate_entry_against_context: bool = True  # re-check entry == close[N] via AnalysisContext


class LiveDataConfig(BaseModel):
    """Live Market Data (Phase 10). Reasonable, un-optimized defaults."""
    # No fresh bar within this window -> STALE, which is a HARD NO_TRADE guard.
    # It measures delivery cadence, not data age, so it must tolerate a delayed
    # 1m feed that arrives in irregular batches: at 90s a single skipped minute
    # marked the feed dead and blocked every signal for the rest of the session.
    # 300s = a few missed 1m bars is survivable, a genuinely dead feed is not.
    stale_after_seconds: float = 300.0
    reconnect_max_attempts: int = 3       # per poll() cycle, before giving up as DISCONNECTED
    reconnect_backoff_seconds: float = 2.0
    buffer_max_days: int = 10             # bound the in-memory 1m buffer size


class RiskConfig(BaseModel):
    """Risk Engine (Phase 7): Signal -> Risk Validation -> Position Size /
    Risk Decision. Reasonable, un-optimized defaults — no parameter sweeps.

    NOTE: this engine NEVER computes a dollar amount unless
    ContractRegistry.require_verified() succeeds for the instrument's specs
    (see signal_engine/risk/engine.py). Nothing here bypasses that guard.
    """
    risk_mode: Literal["fixed", "percent"] = "percent"
    fixed_risk_dollar: float = 500.0        # used when risk_mode == "fixed"
    risk_pct: float = 0.01                  # 1% of account equity (percent mode)
    max_risk_per_trade_dollar: Optional[float] = None  # optional extra hard ceiling
    daily_loss_limit_dollar: float = 1000.0
    max_trades_per_day: int = 5
    max_contracts: int = 10


class DiscordOutputConfig(BaseModel):
    """Discord output adapter (Phase 5.5). The webhook value itself is NEVER
    stored here; only the name of the env var that holds it."""
    enabled: bool = False                    # off by default; core is unaffected
    webhook_env: str = "DISCORD_WEBHOOK_URL"
    timeout_seconds: float = 5.0
    retry_count: int = 2                     # extra attempts on transient errors
    deduplication_enabled: bool = True


class NotificationsConfig(BaseModel):
    """Which trade-lifecycle events reach the operator's phone. Adapter-agnostic
    (Discord today, Telegram tomorrow) — the runtime only decides WHAT to
    announce; adapters decide how to render it."""
    # Announce a trade closing (TP1/TP2/STOP/INVALIDATION) with realised P&L.
    trade_closed: bool = True
    # Announce a trade crossing an R milestone while still open, so the
    # operator knows how far up or down it is without opening the dashboard.
    trade_progress: bool = True
    # R multiples that trigger a progress notification the first time the trade
    # crosses them. Kept deliberately coarse: an alert on every bar is noise the
    # operator will mute, which defeats the point. -1R needs no entry here — a
    # stop-out is a close, not a milestone.
    progress_milestones_r: list[float] = Field(
        default_factory=lambda: [-0.5, 0.5, 1.0, 1.5, 2.0, 3.0])
    # Periodic "everything is fine / here is where we stand" digest, so the
    # operator can confirm the bot is alive while away from home.
    # Off by default: the operator found an hourly message noise. !status in
    # Discord gives the same summary on demand.
    status_digest: bool = False
    status_digest_minutes: float = 60.0      # 0 disables the timer
    # Heads-up "trade idea" before a zone trade confirms, and its cancellation
    # when price crosses the zone without reacting (zone_retrace strategy).
    trade_ideas: bool = True
    # One message when TP1 is hit and the stop moves to breakeven.
    tp1_hit: bool = True
    # One message when breakeven/trailing moves the stop into profit, then
    # again only when the locked-in amount grows by this many R.
    stop_moves: bool = True
    # Attach a chart (zone, entry, planned stop/targets) to the heads-up and
    # the entry message. Needs matplotlib; without it messages go text-only.
    charts: bool = True
    # The day's record (today, week, total, last trades in $) once the
    # 16:55 flat is done — readable on the phone without the dashboard.
    daily_record: bool = True
    stop_move_step_r: float = 0.5


class OutputConfig(BaseModel):
    discord: DiscordOutputConfig = Field(default_factory=DiscordOutputConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)


class ConfluenceWeights(BaseModel):
    """Explainable, CONFIGURABLE contribution weights for GHOST's confluence
    score (Phase 14). The score is NOT a probability of winning — it is the
    relative QUALITY of the conditions. Positive weights add on alignment;
    penalties subtract on conflict."""
    htf_alignment: float = 20.0            # HTF (context) bias matches the signal
    mtf_structure_alignment: float = 15.0  # structure-tf (15m) bias matches
    structure_alignment: float = 15.0      # execution-tf structure matches
    liquidity_sweep: float = 15.0          # a supportive liquidity sweep occurred
    vwap_alignment: float = 10.0           # price on the correct side of VWAP
    volume_confirmation: float = 10.0      # RVOL >= threshold on the decision bar
    rr_quality: float = 12.0               # nearest-target R:R is healthy
    regime_alignment: float = 10.0         # regime favors this setup/direction
    htf_conflict_penalty: float = 25.0     # subtracted when HTF opposes
    against_structure_penalty: float = 20.0  # subtracted when structure opposes


class GhostConfig(BaseModel):
    """GHOST intelligence + paper-memory layer (Phase 14). Purely additive: it
    reads the existing analysis and wraps the existing Setup/Signal engines with
    context + confluence + NO_TRADE + paper-trade memory. It NEVER changes the
    Data/Analysis/Setup/Signal/Risk logic, connects nothing external, and places
    no orders. Disabled -> the runtime behaves exactly as before."""
    enabled: bool = True
    # None disables the confluence gate (the score is still computed and
    # shown) — for strategies whose own entry rules are the whole filter.
    min_confluence_score: Optional[float] = 55.0
    weights: ConfluenceWeights = Field(default_factory=ConfluenceWeights)

    # --- regime classification (deterministic, no-lookahead) --- #
    regime_lookback: int = 40              # bars used to assess trend/range
    regime_slope_lookback: int = 10        # bars for EMA slope
    range_atr_ratio: float = 0.6           # |emaFast-emaSlow| < ratio*ATR => range-ish
    transition_lookback: int = 6           # CHoCH within N bars => TRANSITION
    high_vol_pct: float = 0.80             # ATR percentile above => HIGH volatility
    low_vol_pct: float = 0.20              # below => LOW volatility

    # Reversion setups (liquidity_sweep, vwap_reclaim) fire against the
    # prevailing structure by design — that is their thesis. Charging them the
    # full trend-penalty double-counts the setup's own premise, so the two
    # structural penalties are scaled by this factor for them. 1.0 = no relief.
    reversion_penalty_scale: float = 0.5
    # Ignore swept liquidity older than N execution bars when scoring. 0 = no
    # limit (a sweep from any point in the buffer can still score).
    liquidity_max_age_bars: int = 16
    # EMA bias is NEUTRAL while |ema20-ema50| < this multiple of ATR14, so a
    # hair-thin gap no longer casts a full directional vote. 0 = no dead zone.
    ema_bias_dead_zone_atr: float = 0.15

    # --- NO_TRADE guards --- #
    block_htf_conflict: bool = True        # HTF strongly opposes => NO_TRADE
    block_against_structure: bool = True   # execution structure opposes => NO_TRADE
    max_atr_percentile: float = 0.97       # above => excessive_volatility NO_TRADE
    min_rr: float = 1.0                    # below nearest-target R:R => target_too_close
    stale_after_bars: int = 3              # decision bar older than N bars => stale_data
    # RVOL >= this counts as volume confirmation. 1.0 = "above its own 20-bar
    # average", which splits real volume roughly in half. Do NOT calibrate this
    # against tests/data_samples: that fixture has synthetic volume (3 distinct
    # values), so RVOL there is pinned at 1.000 and tells you nothing.
    rvol_confirm: float = 1.0
    # RTH only by default: the operator does not want positions carried into
    # the overnight Globex session. Combined with eod_close_enabled below, a
    # trade can now only exist inside 09:30-16:00 ET — it opens in RTH (this
    # guard) and is force-flattened the moment RTH ends (the other guard), so
    # neither can be dropped without reopening the overnight-hold gap.
    valid_sessions: list[str] = Field(default_factory=lambda: ["RTH"])

    # --- Phase 15: trade management (per-symbol authority) --- #
    # Stop-too-tight: reject when |entry-stop| < min_stop_atr_mult * ATR14 on the
    # execution timeframe. Does NOT widen the stop silently — rejects with a
    # concrete reason (Opción A). Set to 0 to disable the check.
    min_stop_atr_mult: float = 0.8
    # Stop-too-wide: reject when |entry-stop| > max_stop_atr_mult * ATR14. A
    # reversion setup anchored to a 12-bar swing (vwap_reclaim) can place its
    # stop 7+ ATRs away; at 1:2 R:R that asks price to travel ~15 ATRs, which
    # no intraday bar sequence delivers. Measured on the ES sample, the widest
    # approved stops were 7.56x ATR / 47.8 pts — on NQ (ATR ~20pts) that is the
    # ~150-point stop the operator reported. Set to 0 to disable the check.
    max_stop_atr_mult: float = 1.75
    # Optional absolute per-symbol ceiling on |entry-stop|, in index points.
    # EMPTY BY DEFAULT, deliberately: a fixed points ceiling caps how long a
    # trade may be regardless of conditions, which is exactly what the ATR rule
    # above exists to avoid — a quiet session should get a tight stop and a
    # volatile trend a wide one. Dollar risk is already bounded elsewhere:
    # RiskEngine sizes contracts as floor(max_risk / (stop_points * point_value)),
    # so a wider stop buys fewer contracts rather than more exposure. Populate
    # this only to enforce a hard per-symbol bound that sizing cannot express.
    max_stop_points: dict[str, float] = Field(default_factory=dict)
    # --- regime-adaptive targets --- #
    # The setups build take-profits from a fixed R list, so a trend and a range
    # ask for the same distance. When enabled, GHOST re-places the FAR target
    # according to how well the regime fits the trade (the NEAR target is left
    # alone — banking part of the position early is what makes a far target
    # survivable). See ghost/targets.py.
    # OFF by default because it was measured and it LOST. Replayed through the
    # live tracker on the ES sample (not the TP1-only backtest simulator, which
    # is blind to the far target entirely):
    #   fixed [1R,2R]           20 trades, exp_R 0.500, PF 2.00, net 10.0R
    #   adaptive 3/2/1.5/1      22 trades, exp_R 0.341, PF 1.68, net  7.5R
    #   adaptive 4/2.5/1.5/1    22 trades, exp_R 0.432, PF 1.86, net  9.5R
    # Stretching an aligned trade to 3-4R gives the move room to reverse before
    # it arrives, and the extra stop-outs cost more than the extra reward pays.
    # Kept available, and the trade LENGTH is adaptive regardless: targets are R
    # multiples of a structure-derived stop, so a quiet session produces a short
    # trade and a volatile one a long trade, in points.
    adaptive_targets: bool = False
    # Far-target R by regime-fit band. "aligned" = trading with a established
    # trend, "counter" = against it, where the sensible plan is to take what
    # the move gives and leave.
    target_r_by_band: dict[str, float] = Field(
        default_factory=lambda: {"aligned": 3.0, "favorable": 2.0,
                                 "neutral": 1.5, "counter": 1.0})
    # Extra R allowed when 1H, 15m and 5m all agree with an aligned trade.
    target_r_mtf_bonus: float = 1.0

    # Max simultaneously ACTIVE paper trades per symbol. Default 1 => no
    # LONG+LONG (different setup), no LONG+SHORT, one open per instrument.
    max_open_per_symbol: int = 1
    # Move the stop to entry once TP1 is reached, so a trade running on toward
    # TP2 can no longer give the whole move back. Measured on the ES sample
    # through the live tracker: expectancy 0.500R -> 0.583R, PF 2.00 -> 3.33,
    # net 10.0R -> 14.0R at the same drawdown. See paper/tracker.py.
    breakeven_after_tp1: bool = True
    # Fraction of the position banked at TP1 ("took partials"); the rest runs
    # to TP2 with the stop at breakeven. 0 = all-or-nothing.
    partial_at_tp1: float = 0.0
    # Stop management (paper/tracker.py). Each rule only ever tightens the
    # stop and takes effect from the next bar. None disables it.
    # Breakeven once the trade has run this many R in its favour, with an
    # optional offset in points (e.g. +1 to cover costs).
    be_trigger_r: Optional[float] = None
    be_offset_points: float = 0.0
    # Breakeven once price has covered this fraction of the way from the
    # entry to the final target (0.7 = 70%), with the same points offset.
    # Measured per trade, since the target distance changes with each stop.
    be_at_progress: Optional[float] = None
    # Trailing: after trail_start_r R in favour, the stop follows the best
    # price at trail_distance_r R behind it.
    trail_start_r: Optional[float] = None
    trail_distance_r: float = 1.0
    # Position size used for every dollar figure (Discord, dashboard,
    # backtest, record): `contracts` of the symbol's micro when trade_micro
    # (NQ -> MNQ), else of the symbol itself. Display/accounting only.
    contracts: int = 1
    trade_micro: bool = False
    # Force-flatten any open position the moment the bar's session is no
    # longer RTH (data.sessions classifies the 16:00 ET bar itself as
    # ETH/CLOSED, so this fires AT the 4pm close using that bar's real close
    # price — no lookahead, no invented price). Reuses sessions.rth.end as the
    # single source of truth instead of a second hardcoded clock, so moving
    # the RTH window in data.sessions moves this too. Exists so a trade still
    # open at the close (target/stop not yet hit) never rides into the
    # overnight session, regardless of valid_sessions above.
    eod_close_enabled: bool = True
    # When set (ET "HH:MM"), the force-close waits for this time instead of
    # the end of RTH: CME equity futures keep trading until 17:00 ET, and a
    # prop account (Apex) only requires being flat before 16:59 ET. Closing at
    # 16:00 cut a winning trade an hour early. None = close when RTH ends.
    eod_close_time: Optional[str] = None
    # No new RTH entries at or after this ET time. With the 16:00 force-close
    # above, a 15:50 entry gets 10 minutes to reach targets sized for a normal
    # intraday move — it is closed by the clock, not by the market. None
    # disables the guard.
    last_entry_time: Optional[str] = "15:30"
    # Daily limits on the operator's position (ghost.contracts), counted per
    # trading day (18:00 ET -> 17:00 ET, like Apex). None disables each one.
    # They live here, not in the RiskEngine: that engine stops at the
    # unverified-contract warning and never reaches its own daily checks.
    max_trades_per_day: Optional[int] = None
    daily_loss_limit_usd: Optional[float] = None
    # Cooldown after a losing/invalidating close (in exec-timeframe bars).
    # 0 disables cooldown. Applied per symbol; TP closes do NOT trigger cooldown.
    cooldown_bars: int = 3
    # Exit reasons that trigger cooldown (case-insensitive by value).
    cooldown_on_reasons: list[str] = Field(default_factory=lambda: ["STOP", "INVALIDATION"])

    # --- paper memory --- #
    paper_enabled: bool = True             # create/track PaperTrades from decisions


class EngineConfig(BaseModel):
    version: int = 1
    strategy: str = "legacy"          # set by load(): the overlay in use
    data: DataConfig = Field(default_factory=DataConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    resample: ResampleConfig = Field(default_factory=ResampleConfig)
    rollover: RolloverConfig = Field(default_factory=RolloverConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    timeframe_roles: TimeframeRoles = Field(default_factory=TimeframeRoles)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    setups: SetupsConfig = Field(default_factory=SetupsConfig)
    signals: SignalConfig = Field(default_factory=SignalConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    live: LiveDataConfig = Field(default_factory=LiveDataConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    ghost: GhostConfig = Field(default_factory=GhostConfig)

    @classmethod
    def load(cls, path: Optional[str | Path] = None,
             strategy: Optional[str] = None) -> "EngineConfig":
        """Load default.yaml (or `path`), optionally overlaid with
        config/strategies/<strategy>.yaml. The overlay only lists what the
        strategy changes; everything else keeps the base value."""
        path = Path(path) if path else CONFIG_DIR / "default.yaml"
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}
        if strategy:
            overlay_path = STRATEGIES_DIR / f"{strategy}.yaml"
            if not overlay_path.exists():
                known = sorted(p.stem for p in STRATEGIES_DIR.glob("*.yaml"))
                raise ValueError(f"unknown strategy {strategy!r}; known: {known}")
            with open(overlay_path, "r") as fh:
                raw = _deep_merge(raw, yaml.safe_load(fh) or {})
            raw["strategy"] = strategy
        return cls(**raw)

    @classmethod
    def load_active(cls) -> "EngineConfig":
        """The config the live bot runs: default.yaml + the strategy named by
        $BOT_STRATEGY (default "zones"). "legacy" = default.yaml alone."""
        name = os.environ.get("BOT_STRATEGY", DEFAULT_STRATEGY).strip()
        return cls.load(strategy=None if name in ("", "legacy") else name)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Contract specs
# --------------------------------------------------------------------------- #
class ContractSpec(BaseModel):
    symbol: str
    name: str
    multiplier: float
    tick_size: float
    tick_value: float
    months: list[str]
    exchange: str = "CME"
    currency: str = "USD"
    is_micro: bool = False
    micro_of: Optional[str] = None

    @field_validator("multiplier", "tick_size", "tick_value")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @property
    def point_value(self) -> float:
        """Dollar value of a 1.00 index-point move. Same quantity as
        `multiplier` under a different, commonly-used trading name — exposed
        as an alias (not a stored/duplicated field) so risk/sizing code can
        use whichever term reads better without two sources of truth."""
        return self.multiplier

    def points_to_dollars(self, points: float) -> float:
        return points * self.multiplier

    def ticks_from_points(self, points: float) -> float:
        return points / self.tick_size


class ContractsFile(BaseModel):
    version: int = 1
    verified: bool = False
    verification: dict = Field(default_factory=dict)
    # Symbols whose point value the operator has confirmed. Enough to SHOW
    # dollar figures in messages; the RiskEngine still requires `verified`.
    operator_confirmed: list[str] = Field(default_factory=list)
    month_codes: dict[str, int] = Field(default_factory=dict)
    contracts: dict[str, ContractSpec] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "ContractsFile":
        path = Path(path) if path else CONFIG_DIR / "contracts.yaml"
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}
        # inject symbol into each spec if omitted
        for sym, spec in (raw.get("contracts") or {}).items():
            spec.setdefault("symbol", sym)
        return cls(**raw)
