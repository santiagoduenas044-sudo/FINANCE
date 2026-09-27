"""Tiny local web UI (standard library only)."""
from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, ingest, projections, report, splits, weather
from .query import FIELDS, QueryError

STATIC = Path(__file__).resolve().parent / "static"

EXAMPLES = [
    ("Under con viento", "total_line < 40 and wind >= 12 and outdoors"),
    ("Viento fuerte, cualquier total", "wind >= 15 and outdoors"),
    ("Perro local tras paliza", "home and dog and p_margin <= -14"),
    ("Favorito tras bye", "fav and rest >= 13"),
    ("Divisional, perro de TD+", "div and line >= 7"),
    ("Frío extremo", "temp <= 25 and outdoors"),
    ("Dinero vs tickets (tus splits)", "money_pct >= 60 and bets_pct <= 40"),
]


def _int(qs, key):
    v = qs.get(key, [None])[0]
    return int(v) if v not in (None, "") else None


class Handler(BaseHTTPRequestHandler):
    server_version = "SportsLab/1.0"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, status: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, fn):
        conn = db.connect()
        try:
            self._send(200, fn(conn))
        except (QueryError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # show the error in the UI instead of a dead page
            traceback.print_exc()
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path.startswith("/fonts/"):
            name = path.rsplit("/", 1)[-1]
            font = STATIC / "fonts" / name
            if name.endswith(".woff2") and "/" not in name and font.is_file():
                return self._send(200, font.read_bytes(), "font/woff2")
            return self._send(404, {"error": "no encontrado"})
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
        if path == "/api/meta":
            return self._handle(lambda c: {
                "fields": [{"name": k, "type": t, "desc": d} for k, (t, d) in FIELDS.items()],
                "examples": [{"label": a, "q": b} for a, b in EXAMPLES],
                "seasons": [r[0] for r in c.execute("SELECT DISTINCT season FROM games ORDER BY season")],
                "games": c.execute("SELECT COUNT(*) FROM games").fetchone()[0],
                "model_trained": projections.PARAMS_PATH.exists(),
            })
        if path == "/api/query":
            q = qs.get("q", [""])[0]
            return self._handle(lambda c: report.run(c, q, _int(qs, "desde"), _int(qs, "hasta"), _int(qs, "test_desde")))
        if path == "/api/projections":
            return self._handle(lambda c: projections.weekly(c, _int(qs, "season"), _int(qs, "week")))
        if path == "/api/ratings":
            return self._handle(projections.power_table)
        if path == "/api/backtest":
            def bt(_c):
                if not projections.PARAMS_PATH.exists():
                    raise ValueError("Modelo sin entrenar: python -m sports_lab entrenar")
                return json.loads(projections.PARAMS_PATH.read_text())
            return self._handle(bt)
        if path == "/api/splits":
            return self._handle(splits.latest)
        if path == "/api/upcoming":
            return self._handle(lambda c: [dict(r) for r in c.execute(
                "SELECT game_id, season, week, gameday, gametime, away_team, home_team FROM games "
                "WHERE home_score IS NULL ORDER BY gameday, gametime LIMIT 40")])
        self._send(404, {"error": "no encontrado"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "JSON inválido"})
        if path == "/api/splits":
            def add(c):
                gid = splits.add(c, body.get("game", ""), source=body.get("source") or "manual",
                                 **{k: body.get(k) for k in splits.PCT_COLS})
                return {"ok": True, "game_id": gid}
            return self._handle(add)
        if path == "/api/update":
            def upd(c):
                res = ingest.update(c)
                try:
                    res["weather"] = len(weather.update_forecasts(c))
                    ingest.build_team_games(c)
                except OSError as exc:
                    res["weather_error"] = str(exc)
                return res
            return self._handle(upd)
        if path == "/api/train":
            return self._handle(lambda c: projections.train(c, body.get("test_from")))
        self._send(404, {"error": "no encontrado"})


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Sports Lab en http://{shown}:{port}  (Ctrl+C para salir)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
