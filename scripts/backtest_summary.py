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


def _usd(pts: float | None, pv: float) -> str:
    if pts is None:
        return "—"
    return f"{'+' if pts >= 0 else '−'}${abs(pts * pv):,.0f}"


def _row(label: str, s: dict | None, pv: float | None) -> str:
    """One line in dollars of the position (and points). Old runs saved
    without per-trade points show '—' where the number is not known."""
    if not s:
        return f"  {label:<36} —"
    pv = pv or 1.0
    pts = s.get("pts")
    avg = s.get("avg_pts", pts / s["n"] if pts is not None and s["n"] else None)
    dd = s.get("dd_pts")
    pf = "inf" if s.get("pf") is None else f"{s['pf']:.2f}"
    warn = "  (poca muestra)" if s["n"] < 30 else ""
    return (f"  {label:<36}{s['n']:>5}  gana {s['wr']:>4.0%}  "
            f"prom {_usd(avg, pv):>6}/trade  PF {pf:>5}  neto {_usd(pts, pv):>9}"
            + (f" ({pts:+,.0f} pts)" if pts is not None else "")
            + f"  peor caída {_usd(-dd, pv) if dd is not None else '—'}{warn}")


def _by_year(equity: list[dict]) -> dict[str, dict]:
    """Per-trade points from the cumulative curve, grouped by closing year."""
    out: dict[str, list[float]] = defaultdict(list)
    prev = 0.0
    for p in equity:
        if "cum_pts" not in p:
            return {}                     # run saved before the curve was in points
        out[p["t"][:4]].append(p["cum_pts"] - prev)
        prev = p["cum_pts"]
    res = {}
    for y, ps in sorted(out.items()):
        eq = peak = dd = 0.0
        for v in ps:
            eq += v
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        won, lost = sum(v for v in ps if v > 0), -sum(v for v in ps if v <= 0)
        res[y] = {"n": len(ps), "wr": sum(1 for v in ps if v > 0) / len(ps),
                  "avg_pts": sum(ps) / len(ps), "pts": sum(ps), "dd_pts": dd,
                  "pf": won / lost if lost > 0 else None}
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
    print(f"   (todo en dólares de {m.get('position') or 'la posición'} después de costos · gana = % de"
          " trades ganadores · PF = dólares ganados / dólares perdidos)")

    print("\n-- TOTAL --")
    print(_row("todas", d.get("total"), pv))
    for k, v in (d.get("by_kind") or {}).items():
        print(_row(k, v, pv))
    print("  salidas:", d.get("exits"))

    print("\n-- POR AÑO --")
    years = _by_year(d.get("equity") or [])
    if not years:
        print("  (este backtest es anterior al cambio a puntos; córrelo de nuevo para verlo por año)")
    for y, s in years.items():
        print(_row(y, s, pv))

    print("\n-- POR HORA DE ENTRADA (ET) --")
    for h, s in (d.get("by_hour") or {}).items():
        print(_row(h, s, pv))

    for title, key in (("OBJETIVOS (mismas señales)", "targets"),
                       ("MANEJO DEL STOP (mismas señales)", "policies")):
        print(f"\n-- {title} --")
        items = sorted((d.get(key) or {}).items(),
                       key=lambda kv: -((kv[1] or {}).get("pts") if (kv[1] or {}).get("pts")
                                        is not None else -1e12))
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
