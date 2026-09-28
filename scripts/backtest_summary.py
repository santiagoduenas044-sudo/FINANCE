"""Print a saved backtest (data_store/backtests/*.json) as a short report:
total, by year, by hour, exits, target and stop variants — enough to decide
what to change without opening the dashboard.

    .venv/bin/python scripts/backtest_summary.py                 # newest run
    .venv/bin/python scripts/backtest_summary.py data_store/backtests/noche-5y.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data_store" / "backtests"


def _row(label: str, s: dict | None, pv: float | None) -> str:
    if not s:
        return f"  {label:<36} —"
    pf = "inf" if s.get("pf") is None else f"{s['pf']:.2f}"
    usd = f"  ${s['pts'] * pv:>+10,.0f}" if pv and s.get("pts") is not None else ""
    warn = "  (poca muestra)" if s["n"] < 30 else ""
    return (f"  {label:<36}{s['n']:>5}  WR {s['wr']:>4.0%}  exp {s['exp']:+.3f}R  "
            f"PF {pf:>5}  net {s['net']:+7.1f}R  DD {s['dd']:5.1f}R{usd}{warn}")


def _by_year(equity: list[dict]) -> dict[str, dict]:
    """Per-trade R from the cumulative curve, grouped by the year it closed."""
    out: dict[str, list[float]] = defaultdict(list)
    prev = 0.0
    for p in equity:
        out[p["t"][:4]].append(p["cum"] - prev)
        prev = p["cum"]
    res = {}
    for y, rs in sorted(out.items()):
        eq = peak = dd = 0.0
        for r in rs:
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
        res[y] = {"n": len(rs), "wr": len(wins) / len(rs), "exp": sum(rs) / len(rs),
                  "pf": sum(wins) / -sum(losses) if sum(losses) < 0 else None,
                  "net": sum(rs), "dd": dd}
    return res


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        runs = sorted((p for p in RUNS.glob("*.json") if not p.name.endswith(".progress.json")),
                      key=lambda p: p.stat().st_mtime)
        if not runs:
            print("No hay backtests guardados en data_store/backtests/")
            return 1
        path = runs[-1]
    d = json.loads(path.read_text())
    m = d.get("meta") or {}
    pv = m.get("point_value")
    print(f"== {path.name}: {m.get('symbol')} · {m.get('strategy')} · {m.get('start')} → "
          f"{m.get('end')} · {m.get('sessions')} sesiones · costo {m.get('cost_points')} pts"
          f" · {m.get('position') or ''}")
    print("   (WR = % ganadoras · exp = R promedio por trade · PF = profit factor ·"
          " DD = peor caída en R)")

    print("\n-- TOTAL --")
    print(_row("todas", d.get("total"), pv))
    for k, v in (d.get("by_kind") or {}).items():
        print(_row(k, v, pv))
    print("  salidas:", d.get("exits"))

    print("\n-- POR AÑO --")
    for y, s in _by_year(d.get("equity") or []).items():
        print(_row(y, s, None))

    print("\n-- POR HORA DE ENTRADA (ET) --")
    for h, s in (d.get("by_hour") or {}).items():
        print(_row(h, s, pv))

    for title, key in (("OBJETIVOS (mismas señales)", "targets"),
                       ("MANEJO DEL STOP (mismas señales)", "policies")):
        print(f"\n-- {title} --")
        items = sorted((d.get(key) or {}).items(),
                       key=lambda kv: -(kv[1] or {}).get("net", -1e9))
        for k, v in items:
            print(_row(k, v, pv))

    sh = d.get("shadow") or {}
    print("\n-- FILTRO: aprobadas vs rechazadas --")
    print(_row("aprobadas", sh.get("aprobadas"), pv))
    print(_row("rechazadas", sh.get("rechazadas"), pv))
    f = d.get("funnel") or {}
    print("\n-- EMBUDO --")
    print(f"  candidatas {f.get('candidates')} · puntuadas {f.get('scored')} · "
          f"aprobadas {f.get('approved')}")
    print(f"  rechazos {f.get('rejections')}")
    print(f"  ideas {f.get('ideas')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
