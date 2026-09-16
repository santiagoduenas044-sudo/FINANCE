# Phase 15 — GHOST as authority + real trade management

Goal: turn GHOST into the **single source of truth** for what becomes a trade.
The dashboard, Discord and PaperTrade all consume the *same* final decision;
none of them run ahead of GHOST any more. Real trade management is added on
top: max 1 open per symbol, post-loss cooldown, stop-too-tight rejection with
ATR ratio recorded, and unambiguous TP1/TP2/TP3 with live distances. No new
external APIs, no broker, no orders, no AI/LLM in the path. Data source
unchanged; CME `verified=false` still enforced. Discord stays DISABLED by
default; the routing is proven end-to-end with a capturing adapter in tests.

## Architecture (before → after)

**Before** (Phase 12–14): outputs fired inside the base loop *before* GHOST
even saw the signal — Discord and the dashboard signal log received base
`Signal`s that GHOST would later reject; PaperTrade was the only place that
ever saw the GHOST decision.

**Now (Phase 15)**:

```
DATA → Setup Engine → Signal Engine → base Signal / NoTrade
                                            │
                                        👻 GHOST
                                            │
                          ┌─────────────────┴────────────────┐
                          │                                  │
                       APPROVED                          NO_TRADE
                          │                                  │
             ┌────────────┼────────────┐              GHOST decision log
             ▼            ▼            ▼              (reasons + score
        Dashboard    PaperTrade    Discord             + regime + MTF)
      (recent+chart) (memory)     (when enabled)       + recent_signals row
```

`ghost=None` keeps the pre-Phase-15 behaviour intact (a real regression test
covers it).

## Objectives → what changed

| Objective | Change | Files |
|---|---|---|
| 1. GHOST as authority | Base loop no longer fires `_handle_signal` when `ghost` is enabled — the GHOST path drives both PaperTrade and the output dispatcher. | `runtime/engine.py` |
| 2. Preserve `signal_id` | GHOST reuses the base `Signal` object; the same `signal_id` reaches PaperTrade, Discord and the dashboard. | (no code change; verified by test) |
| 3. NO_TRADE registered | GHOST log persists every rejection with reasons/score/context; each also appears in `recent_signals` for the dashboard table. | `runtime/engine.py`, `api/app.py` (new `/api/ghost/decisions`) |
| 4. Full trade-plan validation | Additional brain guards + explicit `InvalidTradePlan` reason. | `ghost/brain.py`, `ghost/models.py` |
| 5. STOP_TOO_TIGHT | New guard: reject when `|entry-stop| < min_stop_atr_mult · ATR14`. Never widens silently. Records stop, ATR, ratio, threshold. | `ghost/brain.py`, `config/schema.py` |
| 6. Overtrading | `max_open_per_symbol=1` (default). PaperTrade dedup key = `instrument`. Same-bar race guard in runtime. | `paper/models.py`, `paper/tracker.py`, `runtime/engine.py` |
| 7. Cooldown | Post-loss cooldown (`cooldown_bars=3` × exec timeframe). Trigger reasons configurable (default STOP + INVALIDATION). Records `previous_trade_id`, `cooldown_until`, `bars_remaining`. | `paper/tracker.py`, `ghost/brain.py`, `config/schema.py` |
| 8. TP1/TP2/TP3 explicit | `PaperTrade.to_dict()` exposes `tp1`, `tp2`, `tp3`, plus `tp1_r`/`tp2_r`/`tp3_r` and `distance_to_stop`/`distance_to_tp{1,2,3}`. Dashboard renders each as a labelled row with R and pts-away. | `paper/models.py`, `api/static/index.html` |
| 9–10. PaperTrade plan + lifecycle | PaperTrade receives the exact approved `Signal`. All prior lifecycle states are preserved (`PENDING/ACTIVE/TP1_HIT/TP2_HIT/STOPPED/INVALIDATED/CANCELLED/CLOSED`). | (no change) |
| 11. Live P&L | Already computed (points, R, MFE, MAE, duration, bars_held). Now surfaced with distances. Dollar P&L stays blocked (`ContractSpec.verified=false`). | `paper/models.py` |
| 12–13. Dashboard | Active Trade panel now shows TP1/TP2/TP3 with R + distance + HIT flag; `Recent Signals` row for NO_TRADE shows the primary reason as a badge. | `api/static/index.html` |
| 14. Discord | Stays disabled in config; routed only through the APPROVED path so a rejected signal cannot leak. | (no config change) |
| 15. Legacy mode | `ghost=None` keeps Phase 9–13 behaviour exactly. Regression test locks it in. | (verified by test) |
| 16. No duplicates | `PaperTradeTracker._processed_signal_ids` is rebuilt from the on-disk history on load — survives restart. GHOST also checks it as `DUPLICATE` before evaluating. | `paper/tracker.py`, `ghost/brain.py` |
| 17. GhostDecision object | Already existed; extended with `STOP_TOO_TIGHT`, `COOLDOWN`, `ACTIVE_TRADE`, `INVALID_TRADE_PLAN`, `DUPLICATE`. | `ghost/models.py` |

## Trade management rules (defaults)

```yaml
ghost:
  min_stop_atr_mult: 0.8          # reject stops smaller than 0.8× ATR14
  max_open_per_symbol: 1          # one open trade per instrument (any dir/setup)
  cooldown_bars: 3                # post-loss cooldown = 3 × execution tf
  cooldown_on_reasons: [STOP, INVALIDATION]   # winners do NOT trigger cooldown
```

## Empirical validation (ES sample, 400 cycles)

Before Phase 15:
- Discord *would have received* **83** signals.
- GHOST approved only **5** trades.
- **77 rejected signals still went to Discord** (invariant broken).

After Phase 15:
- Discord receives exactly **3** (matches PaperTrades).
- **INVARIANT: `Discord == PaperTrades == GHOST approved` PASSES.**
- Rejection reasons ordered by frequency: `setup_incomplete(80)`,
  `conflicting_setups(63)`, `against_structure(52)`, `stop_too_tight(31)`,
  `insufficient_confluence(12)`, `active_trade(12)`,
  `excessive_volatility(7)`, `cooldown(2)`.

Every one of the observed **~1-point-stop** trades that produced the original
complaint is now rejected as `stop_too_tight` (31 rejections in the sample),
with `stop_distance`, `atr`, `ratio` and `threshold` recorded on each decision.

## Tests

`tests/test_phase15_authority.py` (21 tests): authority (approve/reject
counts, no output bypass, invariant Discord==Paper==approved), lifecycle
(FLAT→ACTIVE, TP1→TP2, STOP, INVALIDATION), overtrading (same-symbol blocks,
NQ not blocked), stop-too-tight (rejected + ATR ratio recorded, wide-enough
allowed, stop never modified), cooldown (STOP triggers, TP doesn't, expiry
works), P&L signs (LONG/SHORT), TP1/TP2/TP3 explicit exposure with distances,
single-target compatibility, restart recovery + duplicate-signal-id block,
legacy `ghost=None` unchanged, integration cases A/B/C/D, `/api/ghost/decisions`.

Full existing suite continues to pass. No engine touched (Data / Market
Analysis / Setup / Signal / Risk).

## Run it

```bash
PYTHONPATH=. python scripts/run_dashboard.py         # dashboard + bot (GHOST on)
PYTHONPATH=. python scripts/demo_ghost.py ES         # historical narrative
```

Discord stays off. To enable it in a future review-and-flip step, set
`output.discord.enabled: true` in config and provide `DISCORD_WEBHOOK_URL`.
GHOST authority guarantees only approved decisions ever reach it.
