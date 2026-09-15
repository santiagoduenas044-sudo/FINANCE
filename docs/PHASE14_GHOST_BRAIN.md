# Phase 14 — GHOST brain: market intelligence + trade memory

Goal: make GHOST (the trading brain inside the FINANCE platform) **think better
and remember better** — interpret market context before deciding, and keep
persistent, self-aware memory of every simulated trade it takes. No new
features for their own sake; priority is clean architecture, traceability, tests
and no-lookahead.

**Hard constraints (all respected):** no TradingView, no broker, no order
execution, no auto-trading, no new external APIs, no new data connections, no
real money, no AI/LLM in the decision path. The data source is unchanged. The
existing engines — Data, Market Analysis, Setup, Signal, Risk — were **not
modified**; GHOST is a layer built strictly on top of their outputs, gated so
that with it disabled the runtime behaves exactly as before. CME
`verified=false` stays enforced (dollar sizing still blocked).

```
market -> GHOST reads context -> detects opportunity -> scores confluence
       -> decides LONG / SHORT / NO_TRADE -> PaperTrade -> remembers + monitors
       -> P&L / R / MFE / MAE -> TP / SL / invalidation -> result -> history/stats
```

## Part 1 — Market intelligence (`signal_engine/ghost/`)

GHOST answers, in structured (machine-readable) data, "what is the market
doing?" before it decides. Everything is derived from data up to the decision
bar (no-lookahead) using objects Market Analysis already produces — nothing is
recomputed or invented.

- **Market regime** (`regime.py`) — deterministic, documented rules over the
  existing EMA 20/50 + ATR + CHoCH: `TREND_UP`, `TREND_DOWN`, `RANGE`,
  `TRANSITION`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `UNKNOWN`, plus a
  volatility bucket from the ATR percentile. The regime influences setup quality
  (a trend breakout scores better than a breakout inside a range).
- **Multi-timeframe** (`context.py::mtf_context`) — 1H context / 15m structure /
  5m execution biases and an `MTFAlignment` (`ALIGNED` / `CONFLICT` /
  `TRANSITION` / `MIXED`). A transition is recognised (a recent CHoCH on the
  structure timeframe), so GHOST does not blindly assume the old direction.
- **Structure** (`context.py::structure_state`) — bias (bullish/bearish/neutral)
  from swings (HH/HL/LH/LL) + last BOS/CHoCH, whether it *just changed*, and
  whether a setup is with or against structure.
- **Liquidity** (`context.py::liquidity_state`) — existed → approached → swept →
  direction → reclaimed? → next un-swept target up/down. Improves the
  liquidity-sweep reading beyond "sweep detected".
- **Key levels** (`context.py::key_levels`) — unified levels (session high/low,
  previous-day high/low, swing highs/lows, equal highs/lows) each with price,
  type, timeframe, active, signed distance, touched, swept.
- **Confluence** (`confluence.py`) — an EXPLAINABLE, CONFIGURABLE score. Not an
  AI confidence and not a win probability — the RELATIVE QUALITY of the
  conditions, as itemised contributions:

  ```
  SHORT score 57 / 55
    +15 structure_alignment (BEARISH LH,HL,LH,HL)
    +15 liquidity_sweep (buy-side swept @ 5008.62, reclaimed)
    +10 vwap_alignment (price BELOW VWAP)
    +10 volume_confirmation (RVOL 1.00)
    +6  rr_quality (R:R 1.00)
    +1  regime_alignment (low volatility)
  ```

  Weights live in `GhostConfig.weights`, so you can see exactly why one trade
  scored 82 and another 54.
- **NO_TRADE** (`brain.py`) — setup detected ≠ trade. Hard guards produce an
  explained NO_TRADE with concrete reasons: `conflicting_timeframe`,
  `against_structure`, `excessive_volatility`, `target_too_close` /
  `insufficient_rr`, `invalid_session`, `conflicting_setups`, `stale_data`,
  `duplicate_active_trade`; and below-threshold confluence gives
  `insufficient_confluence` (with the score still attached). GHOST is never
  forced to emit a signal.

## Part 2 — Trade memory (`signal_engine/paper/`)

GHOST remembers and monitors every trade it takes — **simulated only**, no
broker, no orders.

- **PaperTrade** (`models.py`) — signal id, instrument, direction, setup, entry,
  stop, TP1/TP2, current price, P&L (points), R multiple, MFE, MAE, status,
  timestamps, duration, exit price + reason, and a context snapshot
  (regime/session/confluence).
- **Lifecycle** — `PENDING → ACTIVE → {TP1_HIT, TP2_HIT, STOPPED, INVALIDATED,
  CANCELLED, CLOSED}`, unambiguous (`is_open` is derived; a TP1_HIT trade is
  open only while a TP2 remains).
- **Per-bar update** (`tracker.py`) — every newly closed bar updates P&L, R,
  MFE, MAE and checks TP/SL/invalidation. **OHLC ambiguity** (a bar whose range
  spans both a protective level and a target) resolves CONSERVATIVELY — the
  protective level wins — so a win is never over-reported. This is a documented
  assumption, not fabricated intrabar data.
- **Duplicate protection** — one open trade per (instrument, direction, setup);
  GHOST never re-opens the same trade every candle.
- **Persistence + restart recovery** — open trades (snapshot) and closed trades
  (append-only JSONL) survive a restart, so GHOST still knows its active trade
  after a reboot.
- **Statistics** (`compute_stats`) — totals, wins/losses, win rate, avg R, total
  R, expectancy (avg R — NOT a win probability), avg MFE/MAE, duration, and
  breakdowns by setup / session / regime / direction. Recorded, not acted upon.

**GHOST does not modify its own rules.** It generates trades, records results
and accumulates statistics. Any future auto-optimisation is a separate phase
(and needs careful anti-overfitting).

## Runtime, dashboard, backtest, live

- **Runtime** — `BotRuntime` gains optional `ghost` + `paper_tracker` (both
  `None` ⇒ identical prior behaviour). Each cycle updates active paper trades on
  the closed bar, then GHOST decides and opens paper trades for approved
  decisions. A GHOST failure can never break the trading cycle. Wired in
  `bootstrap.py` from `GhostConfig` (enabled by default; disable via config).
- **Dashboard** — premium design kept; new panels: **👻 Market State** (regime,
  HTF, structure, VWAP, liquidity), **Active Trade** (entry/current/P&L/R/MFE/
  MAE/status), **Confluence** (score + contributions), and **Paper Trades**
  history + **GHOST Stats**. New read-only endpoints: `/api/ghost/market/{sym}`,
  `/api/ghost/trades/active`, `/api/ghost/trades/history`, `/api/ghost/stats`.
- **Backtest = live (same brain)** — the demo (`scripts/demo_ghost.py`) runs the
  identical brain over historical bars as-of each closed bar. A decision at bar
  N is proven identical whether or not future bars exist in the frame
  (`test_no_lookahead_decision_is_asof_only`). Live/paper differs only in where
  the bars come from — there is no second brain.

## Tests

`tests/test_phase14_paper.py` (16) and `tests/test_phase14_ghost.py` (23):
regime (all 7 states), MTF, structure, liquidity, key levels, confluence
(score = Σ contributions, configurable weights), every NO_TRADE reason, signal
quality, PaperTrade creation, active trade, P&L points, R multiple, TP1, TP2,
SL, invalidation, MFE, MAE, duration, duplicate protection, persistence, restart
recovery, OHLC ambiguity, no-lookahead, historical/live consistency, runtime +
API integration, and the "ghost disabled ⇒ unchanged" guarantee. The full
existing suite continues to pass (no regressions).

## Run it

```bash
# Historical narrative (context -> signal -> paper trade -> result + NO_TRADE)
PYTHONPATH=. python scripts/demo_ghost.py ES

# Live/paper: GHOST runs automatically inside the dashboard/bot
PYTHONPATH=. python scripts/run_dashboard.py     # 👻 panels appear when GHOST is on
```

Disable GHOST entirely (runtime identical to Phase 13): set
`ghost.enabled = false` in config.
