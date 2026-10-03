"""Command line: python -m sports_lab <command>

  update                     download NFL data (nflverse) + snapshot current lines + weather
  query "<sdql>" [--sport cfb]   SDQL-style trend (e.g. "team = PIT and p:ATSW and H")
  download cfb --from 2018 [--to 2026]   fetch CFB/NBA/CBB/NHL/MLB from ESPN
  fields                     list every field you can use in a query
  train [--test-from N]      calibrate the model and show the out-of-sample backtest
  projections [--season N --week N]
  ratings                    current power ratings
  weather                    wind/temperature forecast for upcoming games
  splits-add CIN@PIT --home-bets 35 --home-money 60 [--over-bets .. --over-money ..]
  splits-import file.csv
  web [--port 8765] [--host 127.0.0.1]

The original Spanish command names (actualizar, buscar, descargar, ...) still work.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import db, espn, ingest, projections, report, splits, systems, weather
from .query import FIELDS, QueryError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sports_lab", description="Sports Lab")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("update", aliases=["actualizar"], help="download data and rebuild the database")
    a.add_argument("--csv", help="load a local nflverse games.csv instead of downloading it")
    a.add_argument("--no-weather", "--sin-clima", dest="no_weather", action="store_true")

    b = sub.add_parser("query", aliases=["buscar"], help="run an SDQL-style trend query")
    b.add_argument("text")
    b.add_argument("--sport", "--deporte", dest="sport", default="nfl", help="nfl, cfb, nba, cbb, nhl, mlb")
    b.add_argument("--from", "--desde", dest="season_from", type=int, help="first season")
    b.add_argument("--to", "--hasta", dest="season_to", type=int, help="last season")
    b.add_argument("--test-from", "--test-desde", dest="test_from", type=int,
                   help="season where the out-of-sample test starts")
    b.add_argument("--json", action="store_true")

    sub.add_parser("fields", aliases=["campos"], help="list available fields")

    d = sub.add_parser("download", aliases=["descargar"], help="fetch another sport from ESPN")
    d.add_argument("sport", choices=sorted(espn.SPORTS))
    d.add_argument("--from", "--desde", dest="season_from", type=int, required=True,
                   help="first season (the year it starts)")
    d.add_argument("--to", "--hasta", dest="season_to", type=int, help="last season (default: current)")

    e = sub.add_parser("train", aliases=["entrenar"], help="calibrate the model + backtest")
    e.add_argument("--test-from", "--test-desde", dest="test_from", type=int)

    p = sub.add_parser("projections", aliases=["proyecciones"], help="this week's projections")
    p.add_argument("--season", "--temporada", dest="season", type=int)
    p.add_argument("--week", "--semana", dest="week", type=int)
    p.add_argument("--json", action="store_true")

    sub.add_parser("ratings", help="power ratings")
    sub.add_parser("weather", aliases=["clima"], help="refresh the weather forecast")

    s = sub.add_parser("splits-add", aliases=["splits-agregar"], help="store % tickets / % money")
    s.add_argument("game", help="2026_03_CIN_PIT or CIN@PIT")
    s.add_argument("--home-bets", type=float)
    s.add_argument("--home-money", type=float)
    s.add_argument("--over-bets", type=float)
    s.add_argument("--over-money", type=float)
    s.add_argument("--source", "--fuente", dest="source", default="manual")

    si = sub.add_parser("splits-import", aliases=["splits-importar"], help="import splits from a CSV")
    si.add_argument("file")

    w = sub.add_parser("web", help="open the web app")
    w.add_argument("--port", "--puerto", dest="port", type=int, default=8765)
    w.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from your phone/iPad on the same network")

    args = ap.parse_args(argv)
    cmd = {"actualizar": "update", "buscar": "query", "campos": "fields", "descargar": "download",
           "entrenar": "train", "proyecciones": "projections", "clima": "weather",
           "splits-agregar": "splits-add", "splits-importar": "splits-import"}.get(args.cmd, args.cmd)
    conn = db.connect()

    try:
        if cmd == "update":
            text = open(args.csv, encoding="utf-8").read() if args.csv else None
            print("Downloading nflverse data..." if text is None else "Loading local CSV...")
            print(ingest.update(conn, text))
            if text is None:
                try:
                    for r in espn.refresh_recent(conn):
                        print(f"ESPN {r['sport']}: {r['games']} games refreshed")
                    ingest.snapshot_lines(conn)
                    ingest.build_team_games(conn)
                except OSError as exc:
                    print(f"Could not refresh ESPN ({exc})")
            n = systems.grade_picks(conn)
            if n:
                print(f"{n} portfolio picks graded")
            if not args.no_weather:
                _weather(conn)
        elif cmd == "query":
            res = report.run(conn, args.text, args.season_from, args.season_to, args.test_from, sport=args.sport)
            print(json.dumps(res, indent=2, default=str) if args.json else report.format_text(res))
        elif cmd == "fields":
            for k, (_sql, t, desc) in FIELDS.items():
                print(f"  {k:<16} {t:<5} {desc}")
            print("\n  Prefixes: p: previous game, pp: two games ago, o: opponent, op: opponent's previous game")
            print("  Shorthands: H A N F D W L ATSW ATSL ATSP O U")
        elif cmd == "download":
            from datetime import date as _date
            last = args.season_to or _date.today().year
            print(f"Downloading {espn.SPORTS[args.sport]['label']} {args.season_from}-{last} from ESPN "
                  "(one request per day; a season takes 1-2 minutes)...")
            res = espn.download_seasons(conn, args.sport, args.season_from, last,
                                        progress=lambda day, n: print(f"  {day}: {n} games", flush=True))
            print(f"{res['games']} games saved ({res['days']} days). Rebuilding tables...")
            ingest.build_team_games(conn)
            if res["errors"]:
                print(f"{len(res['errors'])} days failed, e.g.: {res['errors'][0]}")
        elif cmd == "train":
            print("Training (walk-forward, takes ~10 s)...")
            print(projections.format_eval(projections.train(conn, args.test_from)))
        elif cmd == "projections":
            res = projections.weekly(conn, args.season, args.week)
            print(json.dumps(res, indent=2, default=str) if args.json else projections.format_weekly(res))
        elif cmd == "ratings":
            for i, r in enumerate(projections.power_table(conn), 1):
                print(f"{i:>2}. {r['team']:<4} {r['rating']:+5.1f}   (results {r['results']:+.1f}, "
                      f"market {r['market']:+.1f}, offense {r['off']:+.1f}, defense {r['def']:+.1f})")
        elif cmd == "weather":
            _weather(conn)
        elif cmd == "splits-add":
            gid = splits.add(conn, args.game, source=args.source,
                             home_bets_pct=args.home_bets, home_money_pct=args.home_money,
                             over_bets_pct=args.over_bets, over_money_pct=args.over_money)
            print(f"Saved for {gid}")
        elif cmd == "splits-import":
            n = splits.import_csv(conn, open(args.file, encoding="utf-8").read())
            print(f"{n} rows imported")
        elif cmd == "web":
            from .server import serve
            serve(args.host, args.port)
    except (QueryError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Network error: {exc}. Check your internet connection.", file=sys.stderr)
        return 1
    return 0


def _weather(conn) -> None:
    try:
        rows = weather.update_forecasts(conn)
    except OSError as exc:
        print(f"Could not fetch the weather ({exc}). NFL data was still updated.")
        return
    ingest.build_team_games(conn)
    windy = [r for r in rows if r.get("wind_mph") is not None]
    print(f"Weather updated for {len(windy)} outdoor games.")
    for r in sorted(windy, key=lambda r: -r["wind_mph"]):
        flag = "  <- strong wind" if r["wind_mph"] >= 12 else ""
        print(f"  {r['matchup']:<12} wind {r['wind_mph']:.0f} mph (gusts {r['gust_mph'] or 0:.0f}), "
              f"{r['temp_f']:.0f}°F, rain {r['precip_mm']} mm{flag}")
    for r in rows:
        if "error" in r:
            print(f"  {r['game_id']}: {r['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
