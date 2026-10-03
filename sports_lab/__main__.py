"""Command line: python -m sports_lab <comando>

  actualizar                 descarga datos NFL (nflverse) + guarda líneas actuales + clima
  buscar "<consulta>" [--deporte cfb]   tendencia estilo SDQL (ej. "team = PIT and p:ATSW and H")
  descargar cfb --desde 2018 [--hasta 2026]   baja CFB/NBA/CBB/NHL/MLB desde ESPN
  campos                     lista de campos que puedes usar en las consultas
  entrenar [--test-desde N]  calibra el modelo y muestra el backtest fuera de muestra
  proyecciones [--temporada N --semana N]
  ratings                    power ratings actuales
  clima                      pronóstico de viento/temperatura de los próximos partidos
  splits-agregar CIN@PIT --home-bets 35 --home-money 60 [--over-bets .. --over-money ..]
  splits-importar archivo.csv
  web [--puerto 8765] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import json
import sys

from . import db, espn, ingest, projections, report, splits, systems, weather
from .query import FIELDS, QueryError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sports_lab", description="Sports Lab NFL")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("actualizar", help="descargar datos y reconstruir la base")
    a.add_argument("--csv", help="usar un games.csv local en vez de descargarlo")
    a.add_argument("--sin-clima", action="store_true")

    b = sub.add_parser("buscar", help="consulta de tendencias")
    b.add_argument("consulta")
    b.add_argument("--deporte", default="nfl", help="nfl, cfb, nba, cbb, nhl, mlb")
    b.add_argument("--desde", type=int, help="temporada inicial")
    b.add_argument("--hasta", type=int, help="temporada final")
    b.add_argument("--test-desde", type=int, help="temporada donde empieza la prueba fuera de muestra")
    b.add_argument("--json", action="store_true")

    sub.add_parser("campos", help="campos disponibles")

    d = sub.add_parser("descargar", help="bajar otro deporte desde ESPN")
    d.add_argument("deporte", choices=sorted(espn.SPORTS))
    d.add_argument("--desde", type=int, required=True, help="primera temporada (año en que empieza)")
    d.add_argument("--hasta", type=int, help="última temporada (por defecto la actual)")

    e = sub.add_parser("entrenar", help="calibrar modelo + backtest")
    e.add_argument("--test-desde", type=int)

    p = sub.add_parser("proyecciones", help="proyecciones de la semana")
    p.add_argument("--temporada", type=int)
    p.add_argument("--semana", type=int)
    p.add_argument("--json", action="store_true")

    sub.add_parser("ratings", help="power ratings")
    sub.add_parser("clima", help="actualizar pronóstico del clima")

    s = sub.add_parser("splits-agregar", help="guardar % tickets / % dinero")
    s.add_argument("partido", help="2026_03_CIN_PIT o CIN@PIT")
    s.add_argument("--home-bets", type=float)
    s.add_argument("--home-money", type=float)
    s.add_argument("--over-bets", type=float)
    s.add_argument("--over-money", type=float)
    s.add_argument("--fuente", default="manual")

    si = sub.add_parser("splits-importar", help="importar splits desde CSV")
    si.add_argument("archivo")

    w = sub.add_parser("web", help="abrir la interfaz web")
    w.add_argument("--puerto", type=int, default=8765)
    w.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para abrirla desde tu celular en la misma red")

    args = ap.parse_args(argv)
    conn = db.connect()

    try:
        if args.cmd == "actualizar":
            text = open(args.csv, encoding="utf-8").read() if args.csv else None
            print("Descargando datos de nflverse..." if text is None else "Cargando CSV local...")
            print(ingest.update(conn, text))
            if text is None:
                try:
                    for r in espn.refresh_recent(conn):
                        print(f"ESPN {r['sport']}: {r['games']} partidos actualizados")
                    ingest.snapshot_lines(conn)
                    ingest.build_team_games(conn)
                except OSError as exc:
                    print(f"No pude actualizar ESPN ({exc})")
            n = systems.grade_picks(conn)
            if n:
                print(f"{n} apuestas de tu portafolio calificadas")
            if not args.sin_clima:
                _clima(conn)
        elif args.cmd == "buscar":
            res = report.run(conn, args.consulta, args.desde, args.hasta, args.test_desde, sport=args.deporte)
            print(json.dumps(res, indent=2, default=str) if args.json else report.format_text(res))
        elif args.cmd == "campos":
            for k, (_sql, t, desc) in FIELDS.items():
                print(f"  {k:<16} {t:<5} {desc}")
            print("\n  Prefijos: p: partido anterior, pp: hace dos, o: rival, op: rival en su partido anterior")
            print("  Atajos: H A N F D W L ATSW ATSL ATSP O U")
        elif args.cmd == "descargar":
            from datetime import date as _date
            last = args.hasta or _date.today().year
            print(f"Descargando {espn.SPORTS[args.deporte]['label']} {args.desde}-{last} desde ESPN "
                  "(un día por petición; una temporada tarda 1-2 minutos)...")
            res = espn.download_seasons(conn, args.deporte, args.desde, last,
                                        progress=lambda day, n: print(f"  {day}: {n} partidos", flush=True))
            print(f"{res['games']} partidos guardados ({res['days']} días). Reconstruyendo tabla...")
            ingest.build_team_games(conn)
            if res["errors"]:
                print(f"{len(res['errors'])} días fallaron, ej.: {res['errors'][0]}")
        elif args.cmd == "entrenar":
            print("Entrenando (walk-forward, tarda ~10 s)...")
            print(projections.format_eval(projections.train(conn, args.test_desde)))
        elif args.cmd == "proyecciones":
            res = projections.weekly(conn, args.temporada, args.semana)
            print(json.dumps(res, indent=2, default=str) if args.json else projections.format_weekly(res))
        elif args.cmd == "ratings":
            for i, r in enumerate(projections.power_table(conn), 1):
                print(f"{i:>2}. {r['team']:<4} {r['rating']:+5.1f}   (resultados {r['results']:+.1f}, "
                      f"mercado {r['market']:+.1f}, ofensiva {r['off']:+.1f}, defensa {r['def']:+.1f})")
        elif args.cmd == "clima":
            _clima(conn)
        elif args.cmd == "splits-agregar":
            gid = splits.add(conn, args.partido, source=args.fuente,
                             home_bets_pct=args.home_bets, home_money_pct=args.home_money,
                             over_bets_pct=args.over_bets, over_money_pct=args.over_money)
            print(f"Guardado para {gid}")
        elif args.cmd == "splits-importar":
            n = splits.import_csv(conn, open(args.archivo, encoding="utf-8").read())
            print(f"{n} filas importadas")
        elif args.cmd == "web":
            from .server import serve
            serve(args.host, args.puerto)
    except (QueryError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error de red: {exc}. Revisa tu conexión a internet.", file=sys.stderr)
        return 1
    return 0


def _clima(conn) -> None:
    try:
        rows = weather.update_forecasts(conn)
    except OSError as exc:
        print(f"No pude obtener el clima ({exc}). Los datos NFL sí se actualizaron.")
        return
    ingest.build_team_games(conn)
    windy = [r for r in rows if r.get("wind_mph") is not None]
    print(f"Clima actualizado para {len(windy)} partidos al aire libre.")
    for r in sorted(windy, key=lambda r: -r["wind_mph"]):
        flag = "  <- viento fuerte" if r["wind_mph"] >= 12 else ""
        print(f"  {r['matchup']:<12} viento {r['wind_mph']:.0f} mph (ráfagas {r['gust_mph'] or 0:.0f}), "
              f"{r['temp_f']:.0f}°F, lluvia {r['precip_mm']} mm{flag}")
    for r in rows:
        if "error" in r:
            print(f"  {r['game_id']}: {r['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
