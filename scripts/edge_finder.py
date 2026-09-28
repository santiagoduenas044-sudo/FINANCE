"""Look for entry filters that hold up out of sample.

Reads the per-signal detail a backtest saves next to its JSON
(data_store/backtests/<run>.shadow.csv, written by audit_real_data.py) and,
for each feature of the signal at entry (trend into the entry, where price
sits in the day's range, zone age, volatility, hour, session...), asks:

  1. TRAIN (before --split): does keeping only the signals with this trait
     earn a positive expectancy after costs, with enough trades to trust it?
  2. TEST (from --split on, never looked at while choosing): does the same
     filter still earn money there?

Only filters that pass BOTH are worth building into the bot. With ~300
filters tried, a handful pass TRAIN by pure chance; the TEST column is what
tells them apart.

    .venv/bin/python scripts/edge_finder.py data_store/backtests/noche-5y2.shadow.csv
"""
from __future__ import annotations

import argparse
import math
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

NUMERIC = {
    "mom_30": "movimiento a favor 30 min antes (pts)",
    "mom_60": "movimiento a favor 1 h antes (pts)",
    "mom_240": "movimiento a favor 4 h antes (pts)",
    "mov_dia": "movimiento a favor en el día (pts)",
    "rango_dia": "posición en el rango del día (0=extremo en contra)",
    "rango_dia_pts": "rango del día (pts)",
    "vela_prom_pts": "tamaño promedio de vela 1m (pts)",
    "zona_ancho_pts": "ancho de la zona (pts)",
    "entrada_vs_mid_pts": "entrada pasada del medio (pts)",
    "objetivo_pts": "distancia al objetivo (pts)",
    "swing_libre_pts": "primer máximo/mínimo libre (pts)",
    "zona_edad_min": "edad de la zona (min)",
    "risk_pts": "stop (pts)",
    "score": "score de confluencia",
    "minute": "minuto del día (ET)",
}
CATEGORICAL = {"direction": "dirección", "session": "sesión", "weekday": "día (0=lunes)",
               "vwap": "lado del VWAP", "approved": "aprobada por GHOST", "hour": "hora"}
QUANTILES = (0.2, 0.4, 0.6, 0.8)


def _t(x: np.ndarray) -> float:
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else 0.0


def _s(x: np.ndarray) -> tuple[int, float, float, float]:
    """n, average points per trade, t-stat, net points."""
    return (len(x), float(x.mean()) if len(x) else 0.0, _t(x), float(x.sum()))


def _filters(train: pd.DataFrame) -> list[tuple[str, callable]]:
    out = []
    for col, name in NUMERIC.items():
        if col not in train or train[col].notna().sum() < 50:
            continue
        for q in QUANTILES:
            v = float(train[col].quantile(q))
            out.append((f"{name} ≤ {v:.2f}", lambda df, c=col, v=v: df[c] <= v))
            out.append((f"{name} ≥ {v:.2f}", lambda df, c=col, v=v: df[c] >= v))
    for col, name in CATEGORICAL.items():
        if col not in train:
            continue
        for val in sorted(train[col].dropna().unique(), key=str):
            out.append((f"{name} = {val}", lambda df, c=col, v=val: df[c] == v))
    return out


PV = 10.0          # $ per point of the position; set from the CSV in main()


def _fmt(n, e, t, net) -> str:
    return (f"{n:>5} prom {'+' if e >= 0 else '−'}${abs(e * PV):,.0f}/trade conf={t:+.1f} "
            f"neto {'+' if net >= 0 else '−'}${abs(net * PV):,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", help="…shadow.csv de un backtest")
    ap.add_argument("--split", default="2024-01-01", help="TRAIN antes, TEST desde (AAAA-MM-DD)")
    ap.add_argument("--min-n", type=int, default=150, help="mínimo de trades en TRAIN")
    ap.add_argument("--cost", type=float, help="costo en pts (default: columna cost_pts o 1.25)")
    a = ap.parse_args()

    df = pd.read_csv(a.csv)
    if "date" not in df:
        print("Este CSV es de un backtest viejo (sin fecha por señal). Corre el backtest otra vez "
              "con la versión actual y usa el nuevo .shadow.csv.")
        return 1
    cost = a.cost if a.cost is not None else float(df["cost_pts"].iloc[0]) \
        if "cost_pts" in df else 1.25
    global PV
    if "usd_per_point" in df and df["usd_per_point"].notna().any():
        PV = float(df["usd_per_point"].dropna().iloc[0])
    df = df[df["risk_pts"] > 0].copy()
    df["hour"] = df["minute"] // 60 if "minute" in df else df["hour"]
    outcomes = {"plan del bot": df["pnl_pts"] - cost}
    if "pnl_2r_pts" in df:
        outcomes["salida simple (TP a 2× el stop)"] = df["pnl_2r_pts"] - cost
    train_m = df["date"] < a.split
    train, test = df[train_m], df[~train_m]
    print(f"== {Path(a.csv).name}: {len(df)} señales · TRAIN {train['date'].min()} → "
          f"{train['date'].max()} ({len(train)}) · TEST {test['date'].min()} → "
          f"{test['date'].max()} ({len(test)}) · costo {cost} pts")
    print(f"   dólares con ${PV:g} por punto, después de costos · conf = qué tan lejos del azar"
          " (≥ 2 ≈ poco probable por suerte)")

    if {"mfe_pts", "mae_pts"} <= set(df):
        print(f"\n-- Qué pasa después de entrar (2 h) --  llega a +20 pts a favor: "
              f"{(df['mfe_pts'] >= 20).mean():.0%} · a +40 pts: {(df['mfe_pts'] >= 40).mean():.0%} · "
              f"va 20 pts en contra: {(df['mae_pts'] >= 20).mean():.0%}")

    filters = _filters(train)
    for oname, r in outcomes.items():
        print(f"\n==================== resultado con: {oname} ====================")
        print(f"  {'SIN FILTRO':<52} TRAIN {_fmt(*_s(r[train_m].dropna().to_numpy()))}"
              f"   TEST {_fmt(*_s(r[~train_m].dropna().to_numpy()))}")
        rows = []
        for label, fn in filters:
            m = fn(df).fillna(False).to_numpy(bool)
            tr = r[m & train_m.to_numpy()].dropna().to_numpy()
            te = r[m & ~train_m.to_numpy()].dropna().to_numpy()
            rows.append((label, m, _s(tr), _s(te)))
        cands = [x for x in rows if x[2][0] >= a.min_n and x[2][1] > 0 and x[2][2] >= 2.0]
        cands.sort(key=lambda x: -x[2][2])
        by_chance = sum(1 for x in rows if x[2][0] >= a.min_n) * 0.023
        print(f"\n  Filtros que ganan en TRAIN (conf ≥ 2, n ≥ {a.min_n}): {len(cands)} "
              f"de {len(rows)} probados (por puro azar se esperan ~{by_chance:.0f})")
        held = 0
        for label, _, tr, te in cands[:25]:
            ok = te[0] >= 30 and te[1] > 0 and te[2] >= 1.0
            held += ok
            print(f"  {'✔' if ok else '✘'} {label:<50} TRAIN {_fmt(*tr)}   TEST {_fmt(*te)}")
        print(f"  → se sostienen en TEST: {held}")

        # pairs of the best single filters: more selective, more chance to overfit
        top = cands[:10]
        pairs = []
        for (la, ma, *_), (lb, mb, *_) in combinations(top, 2):
            m = ma & mb
            tr = r[m & train_m.to_numpy()].dropna().to_numpy()
            te = r[m & ~train_m.to_numpy()].dropna().to_numpy()
            if len(tr) >= a.min_n // 2 and tr.mean() > 0:
                pairs.append((f"{la}  Y  {lb}", _s(tr), _s(te)))
        pairs.sort(key=lambda x: -x[1][2])
        if pairs:
            print("\n  Combinaciones de dos (las 10 mejores en TRAIN):")
            for label, tr, te in pairs[:10]:
                ok = te[0] >= 30 and te[1] > 0 and te[2] >= 1.0
                print(f"  {'✔' if ok else '✘'} {label[:90]}\n      TRAIN {_fmt(*tr)}   TEST {_fmt(*te)}")

    print("\nCómo leerlo: solo un ✔ que gana en TRAIN Y en TEST es una ventaja candidata."
          "\nSi casi todo es ✘, la entrada no tiene ventaja que un filtro simple pueda rescatar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
