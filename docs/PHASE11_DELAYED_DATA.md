# Phase 11 — Delayed Real Market Data

Goal: move the bot from replaying historical ES data (Phase 10) to analyzing
**real** ES/NQ market data, even if delayed, while keeping every rule from
prior phases intact: no auto-trading, no broker connection, no AI in the
core signal path, no TradingView dependency, CME `verified: false` still
blocks any dollar-denominated math.

```
Real Delayed Data → LiveDataAdapter → LiveFeed → Analysis → Setup → Signal → Risk → Dashboard + Discord
```

## 1. Provider research

Priorities given: (1) free/easy for an MVP, (2) reliable, (3) no fragile
scraping, (4) easy to replace later. Explicitly told not to assume
TradingView.

Checked (via curl, HTTP-level reachability): Yahoo Finance (`query1`/
`query2`.finance.yahoo.com), Stooq, Alpha Vantage, Twelve Data, Databento,
Polygon.io, CBOE — from **this development sandbox**, every one of them is
blocked by the sandbox's own egress-proxy allowlist (`403 CONNECT tunnel
failed`), the same failure mode already documented for `cmegroup.com` and
`discord.com` in earlier phases. That restriction is specific to this
sandboxed dev environment, not to wherever the dashboard actually runs
(confirmed: it does not apply on the user's own machine).

**Provider chosen: Yahoo Finance's public chart API**
(`https://query1.finance.yahoo.com/v8/finance/chart/{ticker}`), the same
endpoint the widely-used `yfinance` package wraps.

Why:
- **No API key / no registration** — the most practical free MVP path.
- Delivers 1-minute OHLCV bars for CME index futures via the continuous
  front-month tickers `ES=F` (E-mini S&P 500) and `NQ=F` (E-mini Nasdaq-100)
  — exactly the two instruments required.
- Quotes are explicitly **delayed** for unauthenticated access (Yahoo's own
  UI marks futures quotes "Delayed Quote") — matches "delayed is OK",
  never claims real-time.
- Stable, documented, widely relied upon; trivial to swap out later behind
  the same `LiveDataAdapter` interface (Databento/IBKR/etc. need no changes
  anywhere else).
- No scraping of HTML — it's a JSON API.

No API key setup is needed. Nothing to add to `.env`.

**Transparency about this sandbox:** the real HTTP call to
`query1.finance.yahoo.com` cannot be exercised from *this* development
session (same egress restriction as every other vendor tested — see
`docs/CME_VERIFICATION.md` for the established precedent of documenting
this rather than hiding it). `YahooDelayedAdapter` is implemented against
the endpoint's documented, stable JSON shape and is unit-tested with an
injected fake transport (`transport` constructor parameter — same pattern
as `DiscordOutputAdapter`). Running it against the real network requires a
machine with normal internet access — confirmed to work when the user ran
the dashboard on their own Mac. `run_dashboard.py` automatically falls back
to `SimulatedLiveAdapter` for ES if the real source is unreachable at
startup, so the dashboard still starts either way.

Data frequency/delay: Yahoo does not publish an exact delay figure for
futures; industry-standard unauthenticated delay for CME data is commonly
~10 minutes (some venues advertise up to 20-30 minutes). This adapter does
not claim a specific number — it only asserts "delayed, not real-time" via
`DataFeedStatus.DELAYED`.

## 2. Four-state feed model (LIVE / DELAYED / STALE / DISCONNECTED)

`signal_engine/live/models.py`: `DataFeedStatus` gains `DELAYED`.

`LiveDataAdapter` (`signal_engine/live/adapter.py`) gains a class attribute
`is_delayed: bool = False`. Default `False` means `SimulatedLiveAdapter` and
any adapter that doesn't set it are completely unaffected (still reports
`LIVE`) — no behavior change for Phase 10 code/tests.

`LiveFeed.health()` (`signal_engine/live/feed.py`) now computes:
- `DISCONNECTED` — adapter not connected, or no data ever arrived.
- `STALE` — connected, but no fresh bar within `live.stale_after_seconds`
  (unchanged from Phase 10).
- otherwise: `DELAYED` if `adapter.is_delayed` else `LIVE`.

This is still fully *computed*, never asserted — a delayed source that
stops delivering data still correctly degrades to `STALE`.

## 3. YahooDelayedAdapter

New file: `signal_engine/live/yahoo_adapter.py`.

- Implements the exact `LiveDataAdapter` interface from Phase 10 (`connect`,
  `disconnect`, `is_connected`, `poll`) — nothing upstream changed.
- `is_delayed = True`.
- `connect()` validates reachability with a real request (not just a flag
  flip) so a broken source is caught immediately, not on the first poll.
- `poll(symbol)` fetches `?interval=1m&range=1d`, drops the last (still-
  forming) bar so only **CLOSED** bars are ever returned (no-lookahead,
  same contract as every other adapter), converts epoch-UTC timestamps to
  the configured timezone (`America/New_York`, matching the historical
  pipeline's convention), skips any bar with a null OHLCV field (Yahoo
  reports gaps — e.g. the overnight maintenance break — as nulls), and
  tracks the last-emitted timestamp per symbol so the same bar is never
  resent on the next poll (Yahoo's endpoint returns the whole day's series
  each call).
- Any transport/parsing failure raises `ConnectionError` (never fabricates
  or silently returns stale data) — `LiveFeed`'s existing reconnect/backoff
  logic (Phase 10, unchanged) handles it uniformly.
- `transport` is an injectable constructor parameter (mirrors
  `DiscordOutputAdapter`), so tests never touch the real network.

## 4. run_dashboard.py — real source with automatic fallback

`scripts/run_dashboard.py` now builds a `LiveFeed` per symbol
(`SYMBOLS = ["ES", "NQ"]`):
1. Try `YahooDelayedAdapter.connect()`.
2. If it fails: ES falls back to `SimulatedLiveAdapter` (bundled sample
   data, exactly the Phase 10 behavior) — NQ has no bundled sample data, so
   it is skipped rather than fabricating one; the dashboard still starts
   normally with whatever symbols got a feed.

This satisfies the explicit requirement that `SimulatedLiveAdapter` keep
working as the current fallback.

## 5. Persistent signal record (new, in response to user feedback)

The in-memory `recent_signals` deque (Phase 9) is lost on every restart —
raised directly by the user ("no veo un record"). Added, without touching
Setup/Signal/Risk:

- `BotRuntime` gains an optional `signal_log_path` field. When set, every
  `Signal`/`NoTrade` produced is appended as one JSON line to that file
  (`signal_engine/runtime/engine.py`: `_persist_record`, `read_signal_log`).
  Best-effort: a disk error degrades the `signal_log` component to
  `DEGRADED` but never breaks the trading cycle.
- New endpoint `GET /api/signals/history?limit=N` (`signal_engine/api/
  app.py`) reads that file directly — survives a dashboard restart. Returns
  `{"signals": [], "persisted": false}` when no log path is configured
  (e.g. other scripts/tests that don't need this).
- `run_dashboard.py` points it at `data_store/signal_log.jsonl` (already
  gitignored — same directory Phase 3 uses for Parquet storage).
- Dashboard UI's "Recent Signals" panel now reads from this persisted
  endpoint and shows "(saved to disk — survives restart)" vs. "(in-memory
  only)" so it's never ambiguous which one you're looking at.

## 6. Dashboard UI

`signal_engine/api/static/index.html`:
- New `DELAYED` badge color (blue) alongside existing LIVE/STALE/
  DISCONNECTED.
- Each instrument now shows "Last feed update" (from the new
  `feed_last_update_at` field in `BotRuntime.to_status_dict()`).
- "Last run" (already existed) is the last-analysis timestamp.
- "Recent Signals" now reads persisted history (§5).

## 7. Files touched

- `signal_engine/live/models.py` — `DataFeedStatus.DELAYED`
- `signal_engine/live/adapter.py` — `is_delayed` class attribute
- `signal_engine/live/feed.py` — `health()` computes DELAYED
- `signal_engine/live/yahoo_adapter.py` — new: real (delayed) provider adapter
- `signal_engine/runtime/engine.py` — `feed_last_update_at`,
  `signal_log_path`, `_persist_record`, `read_signal_log`
- `signal_engine/api/app.py` — `GET /api/signals/history`
- `signal_engine/api/static/index.html` — DELAYED badge, last feed update,
  persisted signal history
- `scripts/run_dashboard.py` — real provider with fallback, multi-symbol
  (ES + NQ), signal log wiring
- `tests/test_phase11_delayed_data.py` — new (14 tests)

No changes to `signal_engine/setups/`, `signal_engine/signals/`,
`signal_engine/risk/`, or `signal_engine/backtest/`. CME
`ContractRegistry.require_verified()` untouched and unbypassed.

## 8. Tests

`tests/test_phase11_delayed_data.py` (14 tests, all passing; full suite
299 → 313 passing, no regressions):
- `YahooDelayedAdapter`: ticker mapping, closed-bars-only, no bar resent
  twice, `ConnectionError` on transport failure, `ConnectionError` on
  malformed JSON, `ConnectionError` on `poll()` before `connect()`.
- `LiveFeed.health()`: `DELAYED` for a delayed adapter, still `LIVE` for a
  non-delayed one (Phase 10 regression guard), a delayed source still goes
  `STALE` when data stops arriving.
- Signal persistence: written to disk, readable by a fresh `BotRuntime`
  (simulating a restart), no file created when `signal_log_path` is unset,
  `read_signal_log` returns `[]` for a missing file, `/api/signals/history`
  reports `persisted: false`/`true` correctly and returns the right data.

Full pipeline was also manually exercised end-to-end (dashboard started,
bot activated, ES fell back to Simulated as expected in this sandbox,
signals appeared on the dashboard and were confirmed written to
`data_store/signal_log.jsonl`).

## 9. How to start

```bash
PYTHONPATH=. python scripts/run_dashboard.py
# open http://127.0.0.1:8000/, click Start
```

Startup log tells you, per symbol, whether it's using the real delayed
feed or the simulated fallback. On a machine with normal internet access,
ES and NQ should both connect to Yahoo Finance directly — no configuration
or API key needed.

## 10. Decisions / open questions for review

1. **Exact Yahoo delay is not published** — treated as "delayed, unspecified
   minutes" rather than asserting a number we can't verify. If a stricter
   SLA is needed later, a paid vendor (Databento, Polygon, etc.) is a
   drop-in `LiveDataAdapter` swap.
2. **NQ has no simulated fallback** (no bundled sample file) — by design,
   rather than fabricating fake NQ data. Add a Phase-3-style NQ sample file
   if a simulated NQ fallback becomes important.
3. **Signal persistence** was not in the original Phase 11 spec — added
   because the user pointed out signals were disappearing on restart. It's
   a plain JSONL append log (no rotation/retention policy yet) — fine for
   an MVP but worth revisiting if it runs unattended for a long time.
4. **CME verified=false** continues to block any dollar-denominated Risk
   Engine math, unchanged — this phase only touches market data ingestion.
