"""Stock opportunity scanner — CLI.

Ranks a universe of stocks by a multi-factor score and prints a trade plan
for the best candidates. Optional walk-forward backtest of the ranking.

    PYTHONPATH=. python scripts/scan_stocks.py                       # default universe, Yahoo data
    PYTHONPATH=. python scripts/scan_stocks.py --tickers AAPL,MSFT,NVDA,AMD
    PYTHONPATH=. python scripts/scan_stocks.py --backtest --top 10
    PYTHONPATH=. python scripts/scan_stocks.py --provider synthetic  # offline demo (fake data)
    PYTHONPATH=. python scripts/scan_stocks.py --capital 25000 --risk 0.5 --out reports/

Analysis only: no broker, no orders, not financial advice.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stock_scanner import StockScanner
from stock_scanner.backtest import run_backtest
from stock_scanner.config import ScannerConfig
from stock_scanner.data import make_provider
from stock_scanner.report import backtest_console, to_console, to_json, to_markdown


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Escáner de oportunidades en acciones")
    p.add_argument("--config", help="YAML de configuración (por defecto stock_scanner/config.yaml)")
    p.add_argument("--tickers", help="lista separada por comas; reemplaza el universo del YAML")
    p.add_argument("--provider", choices=["yahoo", "csv", "synthetic"], default="yahoo")
    p.add_argument("--csv-dir", help="carpeta con <TICKER>.csv (provider csv)")
    p.add_argument("--range", default="2y", help="histórico a descargar de Yahoo (1y, 2y, 5y...)")
    p.add_argument("--top", type=int, default=15, help="filas del ranking a mostrar")
    p.add_argument("--capital", type=float, help="capital de la cuenta para calcular el tamaño")
    p.add_argument("--risk", type=float, help="%% del capital arriesgado por operación")
    p.add_argument("--backtest", action="store_true", help="ejecutar backtest walk-forward del ranking")
    p.add_argument("--bt-top", type=int, default=10, help="acciones en cartera en el backtest")
    p.add_argument("--bt-rebalance", type=int, default=21, help="días hábiles entre rebalanceos")
    p.add_argument("--out", help="carpeta donde guardar scan.md y scan.json")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    cfg = ScannerConfig.load(args.config)
    if args.tickers:
        cfg.universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.capital is not None:
        cfg.risk.capital = args.capital
    if args.risk is not None:
        cfg.risk.risk_per_trade_pct = args.risk

    provider = make_provider(args.provider, csv_dir=args.csv_dir, range_=args.range)
    if args.provider == "synthetic":
        print("⚠  Datos SINTÉTICOS (demo offline) — no son precios reales.\n")
    if args.backtest and args.provider == "yahoo" and args.range in ("1y", "6mo", "3mo", "1mo"):
        args.range = "2y"
        provider = make_provider(args.provider, range_="2y")

    scanner = StockScanner(cfg, provider)
    histories = scanner.fetch()
    if not any(t in histories for t in cfg.universe):
        print("No se pudo descargar ningún ticker. ¿Hay conexión a internet / Yahoo Finance?",
              file=sys.stderr)
        return 1

    result = scanner.scan(histories)
    print(to_console(result, top=args.top))

    bt = None
    if args.backtest:
        print()
        try:
            bt = run_backtest(histories, scanner, top_n=args.bt_top, rebalance_days=args.bt_rebalance)
            print(backtest_console(bt))
        except ValueError as exc:
            print(f"Backtest no disponible: {exc}", file=sys.stderr)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "scan.md").write_text(to_markdown(result, top=args.top))
        (out / "scan.json").write_text(to_json(result, bt))
        if bt is not None:
            bt.periods.to_csv(out / "backtest_periods.csv")
        print(f"\nGuardado en {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
