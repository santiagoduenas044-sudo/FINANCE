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
  2. Replays every RTH 5-minute bar through the real BotRuntime (setups ->
     signal engine -> GHOST -> paper tracker, EOD close, breakeven, cooldown),
     with a rolling 5-day history window like the live feed.
  3. Charges a round-trip cost (commission + 1 tick slippage per side).
  4. "Shadow" pass: every signal GHOST scored — approved or not — is simulated
     on its own, to show which setups carry edge, whether the confluence score
     predicts anything, and whether each guard removes losers or winners.

Paper replay only: no broker, no orders, nothing written to data_store/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signal_engine.config.schema import EngineConfig  # noqa: E402
from signal_engine.data.contracts import ContractRegistry  # noqa: E402
from signal_engine.data.engine import DataEngine  # noqa: E402
from signal_engine.data.models import SESSION  # noqa: E402
from signal_engine.data.providers.csv_provider import CsvProvider  # noqa: E402
from signal_engine.ghost.brain import GhostBrain  # noqa: E402
from signal_engine.paper.tracker import PaperTradeTracker  # noqa: E402
from signal_engine.runtime.engine import BotRuntime  # noqa: E402
from signal_engine.setups.engine import SetupEngine  # noqa: E402
from signal_engine.signals.engine import SignalEngine  # noqa: E402

DATA_DIR = ROOT / "market_data"
CONTRACT = "YAHOO"
TICKERS = {"NQ": "NQ=F", "ES": "ES=F"}
# Round trip, in index points: ~$4.50 all-in commission + 1 tick slippage each side.
COST_POINTS = {"NQ": 0.75, "ES": 0.6}
HISTORY_DAYS = 5


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
def replay(symbol: str, provider: CsvProvider, contract: str):
    cfg = EngineConfig.load()
    reg = ContractRegistry.load()
    de = DataEngine(cfg, reg)
    tf_all = de.build_timeframes(de.load_contract(provider, symbol, contract))
    roles = cfg.timeframe_roles
    tfs = [roles.execution, roles.structure, roles.context]
    exec_bars = tf_all[roles.execution]

    tracker = PaperTradeTracker(breakeven_after_tp1=cfg.ghost.breakeven_after_tp1)
    rt = BotRuntime(de, SetupEngine(cfg, reg), SignalEngine(cfg.signals), reg,
                    ghost=GhostBrain(cfg.ghost), paper_tracker=tracker)
    rt.start()

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

    start_at = exec_bars.index[0] + pd.Timedelta(days=HISTORY_DAYS)
    # RTH bars only (the 16:00-labeled bar is RTH by its open instant, and is
    # where the EOD close fires); GHOST never enters outside RTH anyway.
    steps = exec_bars.index[(exec_bars.index >= start_at) & (exec_bars[SESSION] == "RTH")]
    t0 = time.time()
    for i, N in enumerate(steps):
        lo = N - pd.Timedelta(days=HISTORY_DAYS)
        window = {tf: tf_all[tf].loc[lo:N] for tf in tfs}
        rt.run_cycle(symbol, tf_bars=window)
        if i % max(1, len(steps) // 10) == 0:
            print(f"  replay {i / len(steps):4.0%}  {N:%Y-%m-%d %H:%M}  "
                  f"({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)
    return cfg, de, exec_bars, steps, tracker, candidates, decisions


def shadow(cfg, de, exec_bars, decisions):
    """Simulate every scored signal on its own, EOD close included."""
    out = []
    ts_index = exec_bars.index
    for d in decisions:
        s = d.signal
        if s is None or d.confluence is None:
            continue
        tr = PaperTradeTracker(breakeven_after_tp1=cfg.ghost.breakeven_after_tp1)
        t = tr.open_trade(signal_id=s.signal_id, instrument=s.instrument,
                          direction=s.direction.value, setup=s.setup, entry=s.entry,
                          stop=s.stop, take_profits=list(s.take_profits),
                          opened_at=s.timestamp, invalidation_price=s.invalidation_price)
        if t is None:
            continue
        for N in ts_index[ts_index.searchsorted(s.timestamp, side="right"):]:
            tr.update_on_bar(t, exec_bars.loc[N], N)
            if t.is_open and de.classifier.classify(N) != "RTH":
                tr.close_symbol_eod(s.instrument, float(exec_bars.loc[N, "close"]), N)
            if not t.is_open:
                break
        if t.is_open:
            continue                        # ran off the end of the data
        out.append({"setup": s.setup, "direction": s.direction.value,
                    "hour": s.timestamp.hour, "score": d.confluence.score,
                    "approved": d.action != "NO_TRADE",
                    "reasons": [r.value for r in d.no_trade_reasons],
                    "risk_pts": t.initial_risk, "pnl_pts": t.pnl_points,
                    "exit": t.exit_reason.value if t.exit_reason else "?"})
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
        return f"  {label:<26} —"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    usd = f"  ${s['pts'] * pv:>9,.0f}" if pv else ""
    return (f"  {label:<26}{s['n']:>4}  WR {s['wr']:>4.0%}  exp {s['exp']:+.3f}R  "
            f"PF {pf:>5}  net {s['net']:+6.1f}R  DD {s['dd']:4.1f}R{usd}")


def warn(n):
    return "  (!) menos de 30 — no concluyas nada de esta fila" if n < 30 else ""


def report(symbol, cfg, exec_bars, steps, tracker, candidates, decisions, shadow_rows, cost):
    pv = ContractRegistry.load().spec(symbol).point_value
    days = len({ts.date() for ts in steps})
    print(f"\n=== AUDITORÍA CON DATOS REALES — {symbol} ===")
    print(f"Datos: {exec_bars.index[0]:%Y-%m-%d} a {exec_bars.index[-1]:%Y-%m-%d} · "
          f"{days} sesiones RTH reproducidas · costo {cost} pts ida y vuelta "
          f"(${cost * pv:.0f}/contrato)")

    print("\n-- Embudo (RTH) --")
    print("  candidatos por setup: " + ", ".join(f"{k} {v}" for k, v in candidates.most_common()))
    scored = [d for d in decisions if d.signal is not None]
    approved = [d for d in decisions if d.action != "NO_TRADE"]
    print(f"  señales puntuadas por GHOST: {len(scored)} · aprobadas: {len(approved)}")
    reasons = Counter(r.value for d in decisions for r in d.no_trade_reasons)
    print("  rechazos: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common(8)))

    closed = [{"pnl_pts": t.pnl_points, "risk_pts": t.initial_risk, "setup": t.setup,
               "hour": t.opened_at.hour, "exit": t.exit_reason.value}
              for t in tracker.closed_trades]
    print("\n-- Lo que el bot habría operado (una posición a la vez) --")
    s = stats(closed, cost)
    print(line("TOTAL", s, pv) + (warn(s["n"]) if s else ""))
    for k in sorted({x["setup"] for x in closed}):
        print(line(k, stats([x for x in closed if x["setup"] == k], cost), pv))
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
    thr = cfg.ghost.min_confluence_score
    for lo_, hi_ in [(-99, 20), (20, thr), (thr, 50), (50, 999)]:
        rows = [x for x in shadow_rows if lo_ <= x["score"] < hi_]
        print(line(f"score {lo_:>3}..{hi_:<3}", stats(rows, cost)))
    for reason in ["stop_too_tight", "stop_too_wide", "insufficient_confluence",
                   "excessive_volatility", "conflicting_setups", "too_late_in_session"]:
        rows = [x for x in shadow_rows if reason in x["reasons"]]
        if rows:
            print(line(f"rechazo {reason}", stats(rows, cost)))
    print("\nLectura: si 'rechazadas' rinde igual o mejor que 'aprobadas', el filtro de GHOST"
          "\nno está separando nada. Si los scores altos no rinden más que los bajos, el"
          "\nscore de confluencia no predice resultado. Un exp menor que 0 después de costos"
          "\nsignifica que ese setup pierde dinero real.")

    out = DATA_DIR / symbol / "audit_shadow.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(shadow_rows).to_csv(out, index=False)
    print(f"\nDetalle por señal guardado en {out.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NQ", choices=sorted(TICKERS))
    ap.add_argument("--days", type=int, default=29, help="días de historia 1m (Yahoo: máx ~30)")
    ap.add_argument("--offline", action="store_true", help="usar el CSV ya descargado")
    ap.add_argument("--csv-root", help="raíz CsvProvider alternativa (p.ej. tests/data_samples)")
    ap.add_argument("--contract", default=CONTRACT)
    ap.add_argument("--cost-points", type=float, help="costo ida y vuelta en puntos")
    a = ap.parse_args()

    root = Path(a.csv_root) if a.csv_root else DATA_DIR
    if not a.csv_root and not a.offline:
        print(f"Descargando {a.days} días de {TICKERS[a.symbol]} a 1 minuto...", file=sys.stderr)
        print(f"  guardado en {download(a.symbol, a.days).relative_to(ROOT)}", file=sys.stderr)
    cost = a.cost_points if a.cost_points is not None else COST_POINTS[a.symbol]

    cfg, de, exec_bars, steps, tracker, cands, decisions = replay(
        a.symbol, CsvProvider(root), a.contract)
    rows = shadow(cfg, de, exec_bars, decisions)
    report(a.symbol, cfg, exec_bars, steps, tracker, cands, decisions, rows, cost)


if __name__ == "__main__":
    main()
