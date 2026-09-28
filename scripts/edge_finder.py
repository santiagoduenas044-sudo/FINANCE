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
    "mom_30": "tendencia 30 min a favor (R)",
    "mom_60": "tendencia 1 h a favor (R)",
    "mom_240": "tendencia 4 h a favor (R)",
    "mov_dia": "movimiento del día a favor (R)",
    "rango_dia": "posición en el rango del día (0=extremo en contra)",
    "rango_dia_r": "rango del día (R)",
    "atr30_r": "tamaño de vela 1m / stop",
    "zona_ancho_r": "ancho de la zona (R)",
    "entrada_vs_mid_r": "entrada pasada del medio (R)",
    "plan_rr": "R:R del plan",
    "swing_1_r": "primer swing libre (R)",
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
    """n, expectancy R, t-stat, net R."""
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


def _fmt(n, e, t, net) -> str:
    return f"{n:>5} {e:+.3f}R t={t:+.1f} {net:+7.1f}R"


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
    df = df[df["risk_pts"] > 0].copy()
    df["hour"] = df["minute"] // 60 if "minute" in df else df["hour"]
    outcomes = {"plan": (df["pnl_pts"] - cost) / df["risk_pts"]}
    if "pnl_2r_pts" in df:
        outcomes["2R fijo"] = (df["pnl_2r_pts"] - cost) / df["risk_pts"]
    train_m = df["date"] < a.split
    train, test = df[train_m], df[~train_m]
    print(f"== {Path(a.csv).name}: {len(df)} señales · TRAIN {train['date'].min()} → "
          f"{train['date'].max()} ({len(train)}) · TEST {test['date'].min()} → "
          f"{test['date'].max()} ({len(test)}) · costo {cost} pts")
    print("   exp = R promedio por trade después de costos · t = qué tan lejos del azar"
          " (|t| ≥ 2 ≈ poco probable por suerte)")

    if {"mfe_r", "mae_r"} <= set(df):
        print(f"\n-- Qué pasa después de entrar (2 h) --  llega a +1R: "
              f"{(df['mfe_r'] >= 1).mean():.0%} · a +2R: {(df['mfe_r'] >= 2).mean():.0%} · "
              f"va en contra 1R: {(df['mae_r'] >= 1).mean():.0%}")

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
        print(f"\n  Filtros que ganan en TRAIN (t ≥ 2, n ≥ {a.min_n}): {len(cands)} "
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

    print("\nCómo leerlo: solo un ✔ con t alto en TRAIN Y en TEST es una ventaja candidata."
          "\nSi casi todo es ✘, la entrada no tiene ventaja que un filtro simple pueda rescatar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
