"""Run a trend query and summarise it: SU, ATS, moneyline, over/under, by season and out-of-sample."""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

from .query import to_sql

BREAK_EVEN_110 = 110 / 210  # 52.38% needed at -110


def payout(odds) -> float:
    """Profit per 1 unit risked at American odds (defaults to -110)."""
    if odds is None or odds == 0:
        odds = -110
    return odds / 100 if odds > 0 else 100 / -odds


def z_score(wins: int, n: int, p0: float) -> float:
    if n == 0:
        return 0.0
    return (wins - n * p0) / math.sqrt(n * p0 * (1 - p0))


def p_value_one_sided(z: float) -> float:
    """P(result at least this good | true rate = p0)."""
    return 0.5 * math.erfc(z / math.sqrt(2))


class _Tally:
    def __init__(self):
        self.w = self.l = self.p = 0
        self.profit = 0.0

    def add(self, outcome, odds=None):
        if outcome is None:
            self.p += 1
        elif outcome:
            self.w += 1
            self.profit += payout(odds)
        else:
            self.l += 1
            self.profit -= 1

    @property
    def n(self):
        return self.w + self.l

    def as_dict(self, p0=BREAK_EVEN_110):
        n = self.n
        z = z_score(self.w, n, p0)
        return {
            "w": self.w, "l": self.l, "p": self.p, "n": n,
            "pct": round(self.w / n, 4) if n else None,
            "units": round(self.profit, 2),
            "roi": round(self.profit / n, 4) if n else None,
            "z": round(z, 2),
            "p_value": round(p_value_one_sided(z), 4) if n else None,
        }


def _summarise(rows) -> dict:
    su, ats, ml = _Tally(), _Tally(), _Tally()
    over, under = _Tally(), _Tally()
    seen_games = set()
    for r in rows:
        if r["won"] is not None:
            if r["tie"]:
                su.add(None)
                ml.add(None)
            else:
                su.add(bool(r["won"]))
                if r["ml"] is not None:
                    ml.add(bool(r["won"]), r["ml"])
        if r["line"] is not None and r["ats_margin"] is not None:
            ats.add(None if r["ats_push"] else bool(r["covered"]), r["spread_odds"])
        if r["ou_margin"] is not None and r["game_id"] not in seen_games:
            seen_games.add(r["game_id"])
            res = None if r["ou_margin"] == 0 else r["ou_margin"] > 0
            over.add(res, r["over_odds"])
            under.add(None if res is None else not res, r["under_odds"])
    return {
        "su": su.as_dict(0.5), "ats": ats.as_dict(), "ml": ml.as_dict(0.5),
        "over": over.as_dict(), "under": under.as_dict(),
    }


def run(conn: sqlite3.Connection, text: str, season_from: int | None = None,
        season_to: int | None = None, test_from: int | None = None, limit_games: int = 40) -> dict:
    where, params = to_sql(text)
    extra = ""
    if season_from:
        extra += " AND season >= ?"
        params = params + [season_from]
    if season_to:
        extra += " AND season <= ?"
        params = params + [season_to]
    rows = conn.execute(
        f"SELECT * FROM team_games WHERE ({where}){extra} ORDER BY date, game_id, home DESC", params
    ).fetchall()
    played = [r for r in rows if r["won"] is not None]
    upcoming = [r for r in rows if r["won"] is None]

    by_season = defaultdict(list)
    for r in played:
        by_season[r["season"]].append(r)
    seasons = sorted(by_season)

    if test_from is None and seasons:
        # default out-of-sample window: the most recent ~30% of seasons
        test_from = seasons[int(len(seasons) * 0.7)] if len(seasons) >= 4 else None

    both_sides = len({r["game_id"] for r in played}) < len(played)
    warnings = []
    if both_sides:
        warnings.append("El filtro incluye a los dos equipos del mismo partido: el ATS/SU tiende a 50% "
                        "por construcción. Para totales mira Over/Under; para lados agrega home, dog, fav, etc.")
    if len(played) < 100:
        warnings.append(f"Muestra pequeña ({len(played)} filas): un récord así puede salir por azar.")

    out = {
        "query": text, "sql_where": where,
        "rows": len(played), "games": len({r["game_id"] for r in played}),
        "all": _summarise(played),
        "by_season": [{"season": s, **_summarise(by_season[s])} for s in seasons],
        "warnings": warnings,
        "recent": [_row_brief(r) for r in played[-limit_games:]][::-1],
        "upcoming": [_row_brief(r) for r in upcoming],
    }
    if test_from:
        train = [r for r in played if r["season"] < test_from]
        test = [r for r in played if r["season"] >= test_from]
        out["split"] = {"test_from": test_from, "train": _summarise(train), "test": _summarise(test)}
    return out


def _row_brief(r) -> dict:
    return {
        "game_id": r["game_id"], "date": r["date"], "season": r["season"], "week": r["week"],
        "team": r["team"], "opp": r["opp"], "home": r["home"],
        "pts": r["pts"], "opp_pts": r["opp_pts"], "line": r["line"], "total_line": r["total_line"],
        "covered": r["covered"], "ats_push": r["ats_push"], "over": r["over"], "total": r["total"],
        "wind": r["wind"], "wind_fcst": r["wind_fcst"], "temp": r["temp"],
    }


def fmt_record(t: dict, label: str) -> str:
    if not t["n"] and not t["p"]:
        return f"{label:<8} —"
    pct = f"{t['pct']:.1%}" if t["pct"] is not None else "—"
    if label == "SU":  # straight-up record has no price attached
        return f"{label:<8} {t['w']}-{t['l']}-{t['p']}  {pct:>6}"
    roi = f"{t['roi']:+.1%}" if t["roi"] is not None else "—"
    return (f"{label:<8} {t['w']}-{t['l']}-{t['p']}  {pct:>6}  unidades {t['units']:+.1f}  "
            f"ROI {roi:>7}  z={t['z']:+.2f}  p={t['p_value']}")


def format_text(res: dict) -> str:
    a = res["all"]
    lines = [f"Consulta: {res['query']}", f"Partidos: {res['games']}  (filas equipo: {res['rows']})", ""]
    lines += [fmt_record(a["su"], "SU"), fmt_record(a["ats"], "ATS"), fmt_record(a["ml"], "ML"),
              fmt_record(a["over"], "Over"), fmt_record(a["under"], "Under")]
    if "split" in res:
        s = res["split"]
        lines += ["", f"Fuera de muestra (antes de {s['test_from']} vs desde {s['test_from']}):"]
        for k in ("ats", "under", "over"):
            lines.append(f"  {k.upper():<6} antes: {_short(s['train'][k])}   |   desde: {_short(s['test'][k])}")
    lines += ["", "Por temporada (ATS | Over/Under):"]
    for s in res["by_season"]:
        lines.append(f"  {s['season']}: ATS {_short(s['ats'])}   O/U {s['over']['w']}-{s['under']['w']}-{s['over']['p']}")
    if res["upcoming"]:
        lines += ["", "Partidos por jugar que cumplen el filtro:"]
        for r in res["upcoming"]:
            lines.append(f"  {r['date']} {r['team']} vs {r['opp']}  line {r['line']}  total {r['total_line']}"
                         f"  viento pronóstico {r['wind_fcst']}")
    for w in res["warnings"]:
        lines.append("⚠ " + w)
    return "\n".join(lines)


def _short(t: dict) -> str:
    if not t["n"]:
        return "—"
    return f"{t['w']}-{t['l']}-{t['p']} ({t['pct']:.0%}, ROI {t['roi']:+.0%})"
