# Phase 12 — Interactive Chart + Premium Dashboard

Goal: turn the plain control panel from Phases 9–11 into a **premium,
professional trading terminal** with a real interactive candlestick chart —
without touching a single line of the trading engines. Every rule from prior
phases still holds: no auto-trading, no broker, no AI in the signal path, no
TradingView dependency, delayed-data architecture, no-lookahead, and CME
`verified: false` still blocks dollar-denominated math.

```
Real Delayed Data → LiveDataAdapter → LiveFeed → Analysis → Setup → Signal → Risk → Dashboard(+chart) + Discord
```

The whole phase is **additive and read-only**. Nothing in the protected set —
Data Engine, Market Analysis, Setup Engine, Signal Engine, Risk Engine — was
modified. The chart is built by *calling* those exact components, never by
reimplementing or faking them.

## What was NOT changed

- `signal_engine/data/**` (Data Engine)
- `signal_engine/features/**` (Market Analysis, indicators, market structure)
- `signal_engine/setups/**` (Setup Engine)
- `signal_engine/signals/**` (Signal Engine)
- `signal_engine/risk/**` (Risk Engine)

The only Python touched: a new read-only chart module, three new read-only API
routes, a richer (backward-compatible) serialization of signal records, and the
dashboard runner's background loop. The dashboard UI (`index.html`) was
redesigned from scratch.

## 1. The interactive chart — real data, not a mockup

New module: `signal_engine/api/chart.py` → `build_chart_data(runtime, symbol)`.

It is strictly read-only observability and is faithful to the real bot because
it reuses the identical components the trading cycle uses:

- **Candles** come from `LiveFeed.get_tf_bars()[execution_tf]` — the same
  closed, no-lookahead 5-minute bars `run_cycle()` consumes. Nothing is
  fabricated.
- **VWAP / EMA / market structure** come from the Setup Engine's own
  `MarketAnalyzer` (`runtime.setup_engine.analyzer.analyze_view(...)`) and from
  `signal_engine.features.indicators` — the exact causal functions Market
  Analysis uses. EMA 20 and VWAP are read straight from the analysis output;
  EMA 9 is computed with the same `indicators.ema()` (the standard indicator
  set defaults to 20/50, so 9 is added for the chart only).
- **Signal overlays** come from the persisted signal log + in-memory signals
  produced by the Signal Engine — never invented.

Rendered on the chart:

- ES / NQ 5-minute candlesticks with a volume sub-panel.
- Delayed indicator + current/last price + session.
- VWAP, EMA 9, EMA 20 lines (toggleable).
- Liquidity levels (buy-side / sell-side; faded once swept).
- Fair Value Gaps (shaded boxes; dashed border once mitigated).
- BOS / CHoCH structure events.
- Bot entries: LONG / SHORT markers at entry, with entry / stop-loss /
  take-profit level lines projected forward, colored by outcome.
- Trade status per signal: **ACTIVE / WIN / LOSS / INVALIDATED**.

Clicking a signal (marker or table row) opens the detail panel: setup,
direction, entry, SL, TP(s), R:R, reasons, structure/liquidity used,
invalidation conditions, timestamp, and live status.

### Trade-status classification

`_signal_status()` forward-walks only CLOSED bars strictly after a signal:

- **WIN** — first take-profit reached before any protective level.
- **LOSS** — stop hit before the take-profit.
- **INVALIDATED** — a distinct, tighter invalidation level (when the setup
  defined one separate from the stop) breached before stop or target.
- **ACTIVE** — neither target nor protective level touched yet.

Intrabar ties (a bar spanning both TP and stop) resolve conservatively to
LOSS, matching `setups.models.update_status` — the chart never over-reports
wins, preserving no-lookahead honesty.

### The chart is pure observability

Rendering the chart detects no setups, produces no signals, moves no trade
counter, and changes no runtime status. It only reads the feed buffer and runs
analysis for display. Verified by `test_chart_endpoint_does_not_run_trading_pipeline`.

## 2. New API surface (read-only)

- `GET /api/symbols` — symbols that actually have a feed configured (drives the
  chart selector; never advertises a feed that isn't there).
- `GET /api/chart/{symbol}?limit=N` — the chart payload above (`limit` 10–600;
  unknown symbol → 404).
- `GET /api/signals/detail/{signal_id}` — full detail for one signal
  (persisted log first, then in-memory; unknown id → 404).

All existing Phase 9–11 routes are unchanged. There is still **no order /
execute / trade endpoint** of any kind, and responses never contain webhook
secrets (verified by `test_chart_and_detail_never_leak_webhook_secret`).

## 3. Richer, backward-compatible signal records

`runtime.engine._record_to_dict` now serializes the full Signal story
(`signal_id`, `timeframe`, `risk_reward`, `reasons`, `invalidation_conditions`,
`invalidation_price`, `market_context`, `referenced` levels) through a
defensive `_json_safe` converter, so the detail panel works and the full detail
survives a restart via `data_store/signal_log.jsonl`. Existing fields are
untouched, so Phase 11 history keeps working (all Phase 11 tests still pass).

## 4. Fresh chart data even when OFFLINE

`scripts/run_dashboard.py`'s background loop now polls each `LiveFeed` for data
every cycle even when the bot is OFFLINE/PAUSED — this only fetches bars into
the feed buffer so the live chart stays real; it never detects setups,
generates signals, sizes risk, or places orders (the trading pipeline stays
gated by `run_cycle()`'s own ONLINE check, unchanged).

## 5. The redesigned UI

`signal_engine/api/static/index.html` — a self-contained single file, no
external CDN or JS dependency (works fully offline), rendering a custom
canvas-based candlestick chart:

- Dark / graphite background, purple accent, glassy cards, strong hierarchy,
  subtle animations, fully responsive (down to ~400px).
- Layout prioritizes the market/chart: top status bar → symbol tabs + big
  interactive chart → Signals / Risk / Market Status / Bot Control cards →
  Recent Signals + Signal Details.
- Kept and improved from prior phases: ONLINE/OFFLINE/PAUSED/ERROR status,
  per-symbol feed status, DELAYED indicator, last update, recent signals,
  risk today, Start/Pause/Stop, Discord status, component health, signal
  history.
- Chart interactions: crosshair + OHLC/indicator tooltip, layer toggles,
  100/200/400-candle zoom, click-to-select signals.

The feed indicator is *computed*, not asserted — in this dev sandbox (Yahoo
egress blocked → SimulatedLiveAdapter fallback) it honestly shows LIVE/STALE;
on a machine that can reach Yahoo it shows DELAYED. It always reflects the real
feed state.

## Testing

`tests/test_phase12_chart.py` (16 tests) covers: `/api/symbols`,
`/api/chart/{symbol}` structure + OHLC integrity + indicators, unknown-symbol
404, limit validation, no-lookahead, pure-observability (no pipeline side
effects), graceful no-feed handling, signal overlay with status + full detail,
`_signal_status` (WIN/LOSS/ACTIVE/INVALIDATED, intrabar-tie, SHORT),
`/api/signals/detail/{id}`, the enriched JSON-safe record, and secret
non-leakage.

The UI was also manually verified end-to-end with a headless browser
(candles/overlays/markers render, ES↔NQ switch works, Start→ONLINE /
Stop→OFFLINE work, zero console errors).

All prior tests were kept; the full suite is **329 passing** (313 from Phase 11
+ 16 new).

## Run it

```
PYTHONPATH=. python scripts/run_dashboard.py
# then open http://127.0.0.1:8000/
```

The bot still starts OFFLINE — the chart populates from delayed data
immediately; press Start to run the signal pipeline.
