"""Download CME 1-minute bars from Databento for backtesting.

Official CME data (GLBX.MDP3), front-month continuous series (highest volume
contract, rolled automatically), saved in the same format the audit already
reads, so the whole history replays through the real bot:

    pip install databento                       # once
    PYTHONPATH=. .venv/bin/python scripts/fetch_databento.py --start 2025-01-01
    PYTHONPATH=. .venv/bin/python scripts/audit_real_data.py --offline \\
        --contract DATABENTO --start 2026-06-01

The API key is read from $DATABENTO_API_KEY or the DATABENTO_API_KEY line in
.env — it is never printed. The script shows the exact cost Databento will
charge and asks before downloading (--yes skips the question).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "market_data"
CONTRACT = "DATABENTO"
DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"


def api_key() -> str:
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("DATABENTO_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise SystemExit("Falta DATABENTO_API_KEY: agrégala a .env "
                         "(DATABENTO_API_KEY=db-...) y vuelve a correr.")
    return key


def to_bot_csv(df: pd.DataFrame, out: Path) -> Path:
    """Databento bars (UTC index = bar OPEN) -> the audit's CSV: naive ET,
    open-labeled, open/high/low/close/volume."""
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    bars = df[["open", "high", "low", "close", "volume"]].copy()
    bars.index = idx.tz_convert("America/New_York").tz_localize(None)
    bars.index.name = "datetime"
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    out.parent.mkdir(parents=True, exist_ok=True)
    bars.to_csv(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="NQ", help="raíz CME: NQ, ES, MNQ...")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD (default: hoy)")
    ap.add_argument("--yes", action="store_true", help="no preguntar antes de cobrar")
    a = ap.parse_args()
    try:
        import databento as db
    except ImportError:
        raise SystemExit("Instala el cliente primero: .venv/bin/pip install databento")

    end = a.end or pd.Timestamp.now(tz="UTC").normalize().strftime("%Y-%m-%d")
    query = dict(dataset=DATASET, symbols=f"{a.symbol}.v.0", stype_in="continuous",
                 schema=SCHEMA, start=a.start, end=end)
    client = db.Historical(api_key())
    cost = client.metadata.get_cost(**query)
    size_mb = client.metadata.get_billable_size(**query) / 1e6
    print(f"{a.symbol} {SCHEMA} {a.start} → {end}: {size_mb:,.1f} MB, "
          f"costo ${cost:,.2f} (se descuenta del crédito si tienes)")
    if not a.yes and input("¿Descargar? [s/N] ").strip().lower() not in ("s", "si", "sí", "y"):
        print("Cancelado; no se cobró nada.")
        return 0
    df = client.timeseries.get_range(**query).to_df()
    if df.empty:
        raise SystemExit("Databento no devolvió barras para ese rango.")
    out = to_bot_csv(df, DATA_DIR / a.symbol / f"{CONTRACT}_1m.csv")
    print(f"{len(df):,} barras guardadas en {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
