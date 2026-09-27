"""Audit the bot on REAL market data: replay the live pipeline bar by bar.

The bundled tests/data_samples are a Gaussian random walk
(scripts/generate_sample_data.py), where no strategy can have an edge — every
calibration measured on them is fitted to noise. This script is the honest
alternative. Run it on a machine that can reach Yahoo Finance (the Mac the bot
runs on):

    PYTHONPATH=. .venv/bin/python scripts/audit_real_data.py            # NQ, ~29 days
    PYTHONPATH=. .venv/bin/python scripts/audit_real_data.py --offline  # reuse saved CSV

What it does:
  1. Downloads 1-minute NQ=F bars (Yahoo keeps ~30 days of 1m history) and
     saves them to market_data/NQ/YAHOO_1m.csv, so re-runs need no network and
     the file can be shared for analysis.
  2. Replays every RTH execution bar through the real BotRuntime (setups ->
     signal engine -> GHOST -> paper tracker, EOD close, breakeven, cooldown),
     with a rolling history window like the live feed. Uses the same strategy
     the live bot runs ($BOT_STRATEGY, default "zones"; --strategy legacy for
     the old 5m setups).
  3. Charges a round-trip cost (commission + 1 tick slippage per side).
  4. "Shadow" pass: every signal GHOST scored — approved or not — is simulated
     on its own, to show which setups carry edge, whether the confluence score
     predicts anything, and whether each guard removes losers or winners.

Paper replay only: no broker, no orders, nothing written to data_store/.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signal_engine.config.schema import DEFAULT_STRATEGY, EngineConfig  # noqa: E402
from signal_engine.data.contracts import ContractRegistry  # noqa: E402
from signal_engine.data.engine import DataEngine  # noqa: E402
from signal_engine.data.models import SESSION  # noqa: E402
from signal_engine.data.providers.csv_provider import CsvProvider  # noqa: E402
from signal_engine.ghost.brain import GhostBrain  # noqa: E402
from signal_engine.paper.tracker import PaperTradeTracker, whole_contract_partial  # noqa: E402
from signal_engine.runtime.engine import BotRuntime, eod_due, position_usd_per_point  # noqa: E402
from signal_engine.runtime.ideas import IdeaBoard  # noqa: E402
from signal_engine.setups.engine import SetupEngine  # noqa: E402
from signal_engine.signals.engine import SignalEngine  # noqa: E402

DATA_DIR = ROOT / "market_data"
CONTRACT = "YAHOO"
TICKERS = {"NQ": "NQ=F", "ES": "ES=F"}
# Round trip, in index points: ~$4.50 all-in commission + 1 tick slippage each side.
COST_POINTS = {"NQ": 0.75, "ES": 0.6}
# Trading micros instead (MNQ/MES): ~$1.50 round-trip commission+fees per
# contract is 0.75 pt on MNQ ($2/pt), 0.3 pt on MES ($5/pt), + 1 tick each side.
COST_POINTS_MICRO = {"NQ": 1.25, "ES": 0.8}
HISTORY_DAYS = 5          # 5m execution; a 1m execution needs only 2 (see replay)


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def download(symbol: str, days: int) -> Path:
    ticker = TICKERS[symbol]
    end = int(time.time())
    start = end - days * 86400
    rows: dict[int, tuple] = {}
    chunk = 7 * 86400                      # Yahoo serves 1m in <=8-day requests
    t0 = start
    while t0 < end:
        t1 = min(t0 + chunk, end)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval=1m&period1={t0}&period2={t1}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        res = payload["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        for i, ts in enumerate(res.get("timestamp") or []):
            bar = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
            if None not in bar:
                rows[ts] = bar
        t0 = t1
    rows = {ts: b for ts, b in rows.items() if ts < end - 60}   # drop the forming bar
    if not rows:
        raise SystemExit("Yahoo returned no bars — check the internet connection.")
    idx = (pd.to_datetime(sorted(rows), unit="s", utc=True)
           .tz_convert("America/New_York").tz_localize(None))   # naive ET, bar OPEN
    df = pd.DataFrame([rows[t] for t in sorted(rows)], index=idx,
                      columns=["open", "high", "low", "close", "volume"])
    df.index.name = "datetime"
    out = DATA_DIR / symbol / f"{CONTRACT}_1m.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out)
    return out


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def _progress(path: str | None, **fields) -> None:
    """Small JSON the dashboard polls while a backtest runs."""
    if not path:
        return
    try:
        Path(path).write_text(json.dumps(fields))
    except OSError:
        pass


def replay(symbol: str, provider: CsvProvider, contract: str, strategy: str | None,
           start: str | None = None, end: str | None = None,
           progress_file: str | None = None):
    cfg = EngineConfig.load(strategy=strategy)
    reg = ContractRegistry.load()
    de = DataEngine(cfg, reg)
    tf_all = de.build_timeframes(de.load_contract(provider, symbol, contract))
    roles = cfg.timeframe_roles
    tfs = [roles.execution, roles.structure, roles.context]
    exec_bars = tf_all[roles.execution]

    tracker = PaperTradeTracker.from_config(cfg.ghost)
    rt = BotRuntime(de, SetupEngine(cfg, reg), SignalEngine(cfg.signals), reg,
                    ghost=GhostBrain(cfg.ghost), paper_tracker=tracker)
    rt.track_market_state = False   # decisions unchanged; skips idle-bar analysis
    ideas: Counter = Counter()
    if cfg.setups.zone_retrace.enabled:
        rt.idea_board = IdeaBoard(None, timezone=cfg.data.timezone)
        orig_review = rt.idea_board.review

        def review_hook(*a, **k):
            evs = orig_review(*a, **k)
            ideas.update(e["event"] for e in evs)
            return evs
        rt.idea_board.review = review_hook
    rt.start()
    # 1m bars: two days already cover the analysis window (analysis.max_bars)
    # and the 1H EMA50; five would only slow the replay down.
    history_days = 2 if roles.execution == "1m" else HISTORY_DAYS

    candidates: Counter = Counter()
    decisions: list = []
    orig_detect, orig_eval = rt.setup_engine.detect, rt.ghost.evaluate

    def detect_hook(ctx):
        cs = orig_detect(ctx)
        candidates.update(c.setup_name for c in cs)
        return cs

    def eval_hook(*a, **k):
        ms, ds = orig_eval(*a, **k)
        decisions.extend(ds)
        return ms, ds

    rt.setup_engine.detect, rt.ghost.evaluate = detect_hook, eval_hook

    start_at = exec_bars.index[0] + pd.Timedelta(days=history_days)
    # RTH bars only (the 16:00-labeled bar is RTH by its open instant, and is
    # where the EOD close fires); GHOST never enters outside RTH anyway.
    tz = exec_bars.index.tz
    if start:
        start_at = max(start_at, pd.Timestamp(start, tz=tz))
    stop_at = pd.Timestamp(end, tz=tz) + pd.Timedelta(days=1) if end else None
    keep = (exec_bars.index >= start_at) & (exec_bars[SESSION] == "RTH")
    if stop_at is not None:
        keep &= exec_bars.index < stop_at
    steps = exec_bars.index[keep]
    t0 = time.time()
    _progress(progress_file, stage="replay", pct=0.0, at=None, elapsed=0)
    for i, N in enumerate(steps):
        lo = N - pd.Timedelta(days=history_days)
        window = {tf: tf_all[tf].loc[lo:N] for tf in tfs}
        rt.run_cycle(symbol, tf_bars=window)
        if i % max(1, len(steps) // 10) == 0:
            print(f"  replay {i / len(steps):4.0%}  {N:%Y-%m-%d %H:%M}  "
                  f"({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)
        if i % max(1, len(steps) // 200) == 0:
            _progress(progress_file, stage="replay", pct=round(i / len(steps), 4),
                      at=f"{N:%Y-%m-%d %H:%M}", elapsed=round(time.time() - t0),
                      trades=len(tracker.closed_trades))
    return cfg, de, exec_bars, steps, tracker, candidates, decisions, ideas


# Stop-management variants replayed on the SAME approved signals, so the
# table says which rule actually pays on this data instead of guessing.
STOP_POLICIES = {
    "config actual": {},
    "sin BE ni trailing": dict(breakeven_after_tp1=False, be_trigger_r=None,
                               trail_start_r=None),
    "BE a 0.5R": dict(be_trigger_r=0.5),
    "BE a 1R": dict(be_trigger_r=1.0),
    "trailing 1R desde 1R": dict(trail_start_r=1.0, trail_distance_r=1.0),
    "trailing 0.5R desde 1.5R": dict(trail_start_r=1.5, trail_distance_r=0.5),
    "BE 1R + trailing 1R desde 2R": dict(be_trigger_r=1.0, trail_start_r=2.0,
                                         trail_distance_r=1.0),
}


# Target variants on the SAME approved signals: the plan's R is |midpoint -
# stop| (what the message shows); None = the plan as configured.
# A tuple (lo, hi, fallback) is DYNAMIC: the nearest untaken swing between
# lo and hi R, else fallback R — the target adapts to where the structure is.
TARGET_VARIANTS = {
    "plan actual": None,
    "2:1 fijo (1 objetivo)": [2.0],
    "1.5:1 fijo (1 objetivo)": [1.5],
    "3:1 fijo (1 objetivo)": [3.0],
    "TP1 1R + TP 2R": [1.0, 2.0],
    "dinámico 1.5–3R (1 objetivo)": [(1.5, 3.0, 2.0)],
    "TP1 1R + dinámico 1.5–3R": [1.0, (1.5, 3.0, 2.0)],
    "dinámico 2–4R (1 objetivo)": [(2.0, 4.0, 2.5)],
}


# The position split in three (the operator's idea for 5 MNQ): 3 bank TP1,
# 1 goes to the plan's final target, 1 runs with no target. Its own row in
# the targets table, next to the plan as configured.
RUNNER = "3 en TP1 · 1 al final · 1 runner"
RUNNER_LEGS = (
    (0.6, "tp1", {}),
    (0.2, "final", {"be_trigger_r": 1.0}),
    # no target: BE at 1R, trailing 1R once 2R ahead, else the 16:55 flat
    (0.2, "far", {"be_trigger_r": 1.0, "trail_start_r": 2.0, "trail_distance_r": 1.0}),
)


def _runner(s, cfg, arr) -> float | None:
    """Points of the 3/1/1 split per unit of position: each leg simulated on
    its own on the same signal (one target each) and weighted."""
    if not s.take_profits:
        return None
    ctx = s.market_context or {}
    mid = ctx.get("zone_mid", s.entry)
    r = abs(mid - s.stop)
    far = mid + 20 * r if s.direction.value == "LONG" else mid - 20 * r
    tps = {"tp1": s.take_profits[0], "final": s.take_profits[-1], "far": far}
    flat = dict(partial_at_tp1=0.0, breakeven_after_tp1=False, be_trigger_r=None,
                trail_start_r=None)
    total = 0.0
    for w, which, pol in RUNNER_LEGS:
        t = _simulate(replace(s, take_profits=[tps[which]]), cfg, arr, {**flat, **pol})
        if t is None:
            return None
        total += w * t.pnl_points
    return total


def _pick(k, swing_rs: list[float]) -> float:
    if not isinstance(k, tuple):
        return k
    lo, hi, fb = k
    inside = [r for r in swing_rs if lo <= r <= hi]
    return inside[0] if inside else fb


def _with_targets(s, rs: list):
    ctx = s.market_context or {}
    mid = ctx.get("zone_mid", s.entry)
    r = abs(mid - s.stop)
    long = s.direction.value == "LONG"
    ks = [_pick(k, ctx.get("swing_rs") or []) for k in rs]
    ks = sorted(set(ks))
    tps = [mid + k * r if long else mid - k * r for k in ks]
    if (tps[0] <= s.entry) if long else (tps[0] >= s.entry):
        return None                        # fill already past that target
    return replace(s, take_profits=tps)


def _tracker(cfg, policy: dict) -> PaperTradeTracker:
    g = cfg.ghost
    base = dict(breakeven_after_tp1=g.breakeven_after_tp1,
                partial_at_tp1=whole_contract_partial(g.partial_at_tp1, g.contracts),
                be_trigger_r=g.be_trigger_r, be_offset_points=g.be_offset_points,
                trail_start_r=g.trail_start_r, trail_distance_r=g.trail_distance_r)
    return PaperTradeTracker(**{**base, **policy})


class _Arrays:
    def __init__(self, bars):
        self.index = bars.index
        self.h = bars["high"].to_numpy(float)
        self.l = bars["low"].to_numpy(float)
        self.c = bars["close"].to_numpy(float)
        self.session = bars[SESSION].to_numpy()


def _simulate(s, cfg, arr: _Arrays, policy: dict):
    """One signal on its own, from the bar after the signal to its close
    (same end-of-day rule as the bot). None if the data ends first."""
    tr = _tracker(cfg, policy)
    t = tr.open_trade(signal_id=s.signal_id, instrument=s.instrument,
                      direction=s.direction.value, setup=s.setup, entry=s.entry,
                      stop=s.stop, take_profits=list(s.take_profits),
                      opened_at=s.timestamp, invalidation_price=s.invalidation_price)
    if t is None:
        return None
    g = cfg.ghost
    for i in range(arr.index.searchsorted(s.timestamp, side="right"), len(arr.index)):
        ts = arr.index[i]
        tr.update_on_bar(t, {"high": arr.h[i], "low": arr.l[i], "close": arr.c[i]}, ts)
        if t.is_open and g.eod_close_enabled and eod_due(arr.session[i], ts, g.eod_close_time):
            tr.close_symbol_eod(s.instrument, float(arr.c[i]), ts)
        if not t.is_open:
            return t
    return None


def shadow(cfg, de, exec_bars, decisions):
    """Simulate every scored signal on its own, EOD close included; approved
    ones also under each STOP_POLICIES variant."""
    out = []
    arr = _Arrays(exec_bars)
    for d in decisions:
        s = d.signal
        if s is None or d.confluence is None:
            continue
        t = _simulate(s, cfg, arr, {})
        if t is None:
            continue                        # ran off the end of the data
        row = {"setup": s.setup, "direction": s.direction.value,
               "hour": s.timestamp.hour, "score": d.confluence.score,
               "approved": d.action != "NO_TRADE",
               "reasons": [r.value for r in d.no_trade_reasons],
               "risk_pts": t.initial_risk, "pnl_pts": t.pnl_points,
               "exit": t.exit_reason.value if t.exit_reason else "?", "policy_pnl": {},
               "target_pnl": {}}
        if row["approved"]:
            for name, pol in STOP_POLICIES.items():
                v = _simulate(s, cfg, arr, pol) if pol else t
                if v is not None:
                    row["policy_pnl"][name] = v.pnl_points
            for name, rs in TARGET_VARIANTS.items():
                s2 = s if rs is None else _with_targets(s, rs)
                v = None if s2 is None else (t if rs is None else _simulate(s2, cfg, arr, {}))
                if v is not None:
                    row["target_pnl"][name] = v.pnl_points
            v = _runner(s, cfg, arr)
            if v is not None:
                row["target_pnl"][RUNNER] = v
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def stats(rows, cost):
    """rows: dicts with pnl_pts, risk_pts. Returns summary with costs applied."""
    if not rows:
        return None
    r = [(x["pnl_pts"] - cost) / x["risk_pts"] for x in rows]
    wins = [v for v in r if v > 0]
    losses = [v for v in r if v <= 0]
    eq = peak = dd = 0.0
    for v in r:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = sum(wins) / -sum(losses) if sum(losses) < 0 else float("inf")
    return {"n": len(r), "wr": len(wins) / len(r), "exp": sum(r) / len(r),
            "pf": pf, "net": sum(r), "dd": dd,
            "pts": sum(x["pnl_pts"] - cost for x in rows)}


def line(label, s, pv=None):
    if s is None:
        return f"  {label:<30} —"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    usd = f"  ${s['pts'] * pv:>9,.0f}" if pv else ""
    return (f"  {label:<30}{s['n']:>4}  WR {s['wr']:>4.0%}  exp {s['exp']:+.3f}R  "
            f"PF {pf:>5}  net {s['net']:+6.1f}R  DD {s['dd']:4.1f}R{usd}")


def warn(n):
    return "  (!) menos de 30 — no concluyas nada de esta fila" if n < 30 else ""


def position(cfg, symbol) -> tuple[str, float]:
    """(label, $ per point) of the position every $ figure is computed for,
    e.g. ("5 MNQ", 10.0)."""
    reg = ContractRegistry.load()
    pos = position_usd_per_point(reg, cfg.ghost, symbol)
    if pos:
        return next(iter(pos.items()))
    return f"1 {symbol}", reg.spec(symbol).point_value


def report(symbol, cfg, exec_bars, steps, tracker, candidates, decisions, shadow_rows,
           cost, ideas, shadow_path: Path | None = None):
    label, pv = position(cfg, symbol)
    days = len({ts.date() for ts in steps})
    print(f"\n=== AUDITORÍA CON DATOS REALES — {symbol} ===")
    print(f"Datos: {exec_bars.index[0]:%Y-%m-%d} a {exec_bars.index[-1]:%Y-%m-%d} · "
          f"{days} sesiones RTH reproducidas · costo {cost} pts ida y vuelta · "
          f"$ calculados para {label} (${pv:g}/pt)")

    print("\n-- Embudo (RTH) --")
    print("  candidatos por setup: " + ", ".join(f"{k} {v}" for k, v in candidates.most_common()))
    scored = [d for d in decisions if d.signal is not None]
    approved = [d for d in decisions if d.action != "NO_TRADE"]
    print(f"  señales puntuadas por GHOST: {len(scored)} · aprobadas: {len(approved)}")
    reasons = Counter(r.value for d in decisions for r in d.no_trade_reasons)
    print("  rechazos: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common(8)))

    if ideas or cfg.setups.zone_retrace.enabled:
        print(f"  ideas avisadas (🟡): {ideas['idea']} · canceladas (⚪): {ideas['cancelled']}")
    closed = [{"pnl_pts": t.pnl_points, "risk_pts": t.initial_risk, "setup": t.setup,
               "hour": t.opened_at.hour, "exit": t.exit_reason.value,
               "direct": bool((t.context or {}).get("idea_direct"))}
              for t in tracker.closed_trades]
    print("\n-- Lo que el bot habría operado (una posición a la vez) --")
    s = stats(closed, cost)
    print(line("TOTAL", s, pv) + (warn(s["n"]) if s else ""))
    for k in sorted({x["setup"] for x in closed}):
        print(line(k, stats([x for x in closed if x["setup"] == k], cost), pv))
    if cfg.setups.zone_retrace.enabled:
        print(line("con aviso previo (🟢)", stats([x for x in closed if not x["direct"]], cost), pv))
        print(line("señal directa (⚡)", stats([x for x in closed if x["direct"]], cost), pv))
    print("  salidas: " + ", ".join(f"{k} {v}" for k, v in
                                    Counter(x["exit"] for x in closed).most_common()))
    by_hour = defaultdict(list)
    for x in closed:
        by_hour[x["hour"]].append(x)
    for h in sorted(by_hour):
        print(line(f"entrada {h:02d}:00-{h:02d}:59", stats(by_hour[h], cost)))

    print("\n-- Sombra: TODAS las señales puntuadas, cada una por separado --")
    print(line("todas", stats(shadow_rows, cost)))
    print(line("  aprobadas por GHOST", stats([x for x in shadow_rows if x["approved"]], cost)))
    print(line("  rechazadas por GHOST", stats([x for x in shadow_rows if not x["approved"]], cost)))
    for k in sorted({x["setup"] for x in shadow_rows}):
        print(line(f"setup {k}", stats([x for x in shadow_rows if x["setup"] == k], cost)))
    thr = cfg.ghost.min_confluence_score or 35   # buckets still useful without a gate
    for lo_, hi_ in [(-99, 20), (20, thr), (thr, 50), (50, 999)]:
        rows = [x for x in shadow_rows if lo_ <= x["score"] < hi_]
        print(line(f"score {lo_:>3}..{hi_:<3}", stats(rows, cost)))
    for reason in ["stop_too_tight", "stop_too_wide", "insufficient_confluence",
                   "excessive_volatility", "conflicting_setups", "too_late_in_session"]:
        rows = [x for x in shadow_rows if reason in x["reasons"]]
        if rows:
            print(line(f"rechazo {reason}", stats(rows, cost)))
    print("\n-- Objetivos (R:R): mismas señales aprobadas --")
    for name in [*TARGET_VARIANTS, RUNNER]:
        rows = [{"pnl_pts": x["target_pnl"][name], "risk_pts": x["risk_pts"]}
                for x in shadow_rows if name in x.get("target_pnl", {})]
        print(line(name, stats(rows, cost), pv))
    print("\n-- Manejo del stop: mismas señales aprobadas, distintas reglas --")
    for name in STOP_POLICIES:
        rows = [{"pnl_pts": x["policy_pnl"][name], "risk_pts": x["risk_pts"]}
                for x in shadow_rows if name in x.get("policy_pnl", {})]
        print(line(name, stats(rows, cost), pv))
    print("\nLectura: si 'rechazadas' rinde igual o mejor que 'aprobadas', el filtro de GHOST"
          "\nno está separando nada. Si los scores altos no rinden más que los bajos, el"
          "\nscore de confluencia no predice resultado. Un exp menor que 0 después de costos"
          "\nsignifica que ese setup pierde dinero real.")

    out = shadow_path or DATA_DIR / symbol / "audit_shadow.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(shadow_rows).to_csv(out, index=False)
    print(f"\nDetalle por señal guardado en {out}")


def _json_safe(x):
    if isinstance(x, float):
        return None if x != x or x in (float("inf"), float("-inf")) else round(x, 4)
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    return x


def summary(symbol, cfg, strategy, contract, steps, tracker, candidates, decisions,
            shadow_rows, cost, ideas) -> dict:
    """Everything report() prints, as data for the dashboard's backtest view."""
    label, pv = position(cfg, symbol)
    closed = sorted(tracker.closed_trades, key=lambda t: t.closed_at)
    rows = [{"pnl_pts": t.pnl_points, "risk_pts": t.initial_risk, "hour": t.opened_at.hour,
             "exit": t.exit_reason.value,
             "direct": bool((t.context or {}).get("idea_direct"))} for t in closed]
    cum, equity, trades = 0.0, [], []
    for t in closed:
        r = (t.pnl_points - cost) / t.initial_risk
        cum += r
        equity.append({"t": t.closed_at.isoformat(), "cum": cum})
        trades.append({"opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat(),
                       "direction": t.direction, "entry": t.entry,
                       "exit_price": t.exit_price, "exit": t.exit_reason.value,
                       "pnl_pts": t.pnl_points - cost, "r": r,
                       "idea": (t.context or {}).get("idea_number"),
                       "direct": bool((t.context or {}).get("idea_direct"))})
    by_hour = defaultdict(list)
    for x in rows:
        by_hour[x["hour"]].append(x)
    approved = [x for x in shadow_rows if x["approved"]]
    return {
        "meta": {"symbol": symbol, "strategy": strategy, "data": contract,
                 "start": steps[0].strftime("%Y-%m-%d") if len(steps) else None,
                 "end": steps[-1].strftime("%Y-%m-%d") if len(steps) else None,
                 "sessions": len({ts.date() for ts in steps}), "cost_points": cost,
                 "point_value": pv, "position": label,
                 "generated_at": pd.Timestamp.now(tz="America/New_York").isoformat()},
        "funnel": {"candidates": dict(candidates),
                   "scored": sum(1 for d in decisions if d.signal is not None),
                   "approved": sum(1 for d in decisions if d.action != "NO_TRADE"),
                   "rejections": dict(Counter(r.value for d in decisions
                                              for r in d.no_trade_reasons).most_common(10)),
                   "ideas": dict(ideas)},
        "total": stats(rows, cost),
        "by_kind": {"con aviso": stats([x for x in rows if not x["direct"]], cost),
                    "directa": stats([x for x in rows if x["direct"]], cost)},
        "by_hour": {f"{h:02d}:00": stats(v, cost) for h, v in sorted(by_hour.items())},
        "exits": dict(Counter(x["exit"] for x in rows)),
        "shadow": {"aprobadas": stats(approved, cost),
                   "rechazadas": stats([x for x in shadow_rows if not x["approved"]], cost)},
        "policies": {name: stats([{"pnl_pts": x["policy_pnl"][name], "risk_pts": x["risk_pts"]}
                                  for x in approved if name in x["policy_pnl"]], cost)
                     for name in STOP_POLICIES},
        "targets": {name: stats([{"pnl_pts": x["target_pnl"][name], "risk_pts": x["risk_pts"]}
                                 for x in approved if name in x.get("target_pnl", {})], cost)
                    for name in [*TARGET_VARIANTS, RUNNER]},
        "equity": equity,
        "trades": trades[-500:],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NQ", choices=sorted(TICKERS))
    ap.add_argument("--days", type=int, default=29, help="días de historia 1m (Yahoo: máx ~30)")
    ap.add_argument("--offline", action="store_true", help="usar el CSV ya descargado")
    ap.add_argument("--csv-root", help="raíz CsvProvider alternativa (p.ej. tests/data_samples)")
    ap.add_argument("--contract", default=CONTRACT)
    ap.add_argument("--cost-points", type=float, help="costo ida y vuelta en puntos")
    ap.add_argument("--strategy", default=os.environ.get("BOT_STRATEGY", DEFAULT_STRATEGY),
                    help='estrategia (default la del bot: "zones"; "legacy" = setups 5m)')
    ap.add_argument("--start", help="replay desde YYYY-MM-DD (datos largos: ve por tramos)")
    ap.add_argument("--end", help="replay hasta YYYY-MM-DD, inclusive")
    ap.add_argument("--json-out", help="guardar el resumen en JSON (dashboard)")
    ap.add_argument("--progress-file", help="JSON de avance mientras corre (dashboard)")
    a = ap.parse_args()
    strategy = None if a.strategy in ("", "legacy") else a.strategy

    root = Path(a.csv_root) if a.csv_root else DATA_DIR
    if not a.csv_root and not a.offline:
        print(f"Descargando {a.days} días de {TICKERS[a.symbol]} a 1 minuto...", file=sys.stderr)
        print(f"  guardado en {download(a.symbol, a.days).relative_to(ROOT)}", file=sys.stderr)
    cfg, de, exec_bars, steps, tracker, cands, decisions, ideas = replay(
        a.symbol, CsvProvider(root), a.contract, strategy, a.start, a.end, a.progress_file)
    cost = a.cost_points if a.cost_points is not None else \
        (COST_POINTS_MICRO if cfg.ghost.trade_micro else COST_POINTS)[a.symbol]
    _progress(a.progress_file, stage="shadow", pct=1.0)
    rows = shadow(cfg, de, exec_bars, decisions)
    report(a.symbol, cfg, exec_bars, steps, tracker, cands, decisions, rows, cost, ideas,
           shadow_path=Path(a.json_out).with_suffix(".shadow.csv") if a.json_out else None)
    if a.json_out:
        data = summary(a.symbol, cfg, a.strategy, a.contract, steps, tracker, cands,
                       decisions, rows, cost, ideas)
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(json.dumps(_json_safe(data)))
    _progress(a.progress_file, stage="done", pct=1.0)


if __name__ == "__main__":
    main()
