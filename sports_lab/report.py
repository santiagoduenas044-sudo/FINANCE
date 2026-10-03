"""Run a trend query and summarise it: SU, ATS, moneyline, over/under, by season and out-of-sample."""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

from .query import compile_query

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


TEASER_POINTS = (-10, -6, 6, 10)


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _rec(margins) -> dict:
    w = sum(1 for m in margins if m > 0)
    l = sum(1 for m in margins if m < 0)
    p = sum(1 for m in margins if m == 0)
    return {"w": w, "l": l, "p": p, "pct": round(w / (w + l), 4) if w + l else None}


def _summarise(rows, teasers: bool = False) -> dict:
    su, ats, ml = _Tally(), _Tally(), _Tally()
    over, under = _Tally(), _Tally()
    seen_games = set()
    ou_rows = []
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
            ou_rows.append(r)
            res = None if r["ou_margin"] == 0 else r["ou_margin"] > 0
            over.add(res, r["over_odds"])
            under.add(None if res is None else not res, r["under_odds"])
    out = {
        "su": su.as_dict(0.5), "ats": ats.as_dict(), "ml": ml.as_dict(0.5),
        "over": over.as_dict(), "under": under.as_dict(),
        "avg": {
            "margin": _avg(r["margin"] for r in rows),
            "ats_margin": _avg(r["ats_margin"] for r in rows),
            "line": _avg(r["line"] for r in rows),
            "ou_margin": _avg(r["ou_margin"] for r in ou_rows),
            "total_line": _avg(r["total_line"] for r in ou_rows),
            "points": _avg(r["pts"] for r in rows),
            "opp_points": _avg(r["opp_pts"] for r in rows),
        },
    }
    if teasers:
        # move the number by k points: ATS at (team line + k), Over/Under at (total + k)
        ats_m = [r["ats_margin"] for r in rows if r["ats_margin"] is not None]
        ou_m = [r["ou_margin"] for r in ou_rows]
        out["teasers"] = [{"points": k, "ats": _rec([m + k for m in ats_m]),
                           "over": _rec([m - k for m in ou_m]), "under": _rec([k - m for m in ou_m])}
                          for k in TEASER_POINTS]
    return out


def run(conn: sqlite3.Connection, text: str, season_from: int | None = None,
        season_to: int | None = None, test_from: int | None = None, limit_games: int = 300,
        sport: str = "nfl") -> dict:
    q = compile_query(text)
    params = [sport] + q.params
    extra = ""
    if season_from:
        extra += " AND t.season >= ?"
        params.append(season_from)
    if season_to:
        extra += " AND t.season <= ?"
        params.append(season_to)
    rows = conn.execute(
        f"SELECT t.* FROM team_games t {' '.join(q.joins)} WHERE t.sport = ? AND ({q.where}){extra} "
        "ORDER BY t.date, t.game_id, t.home DESC", params
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
        warnings.append("This filter includes both teams of the same game, so ATS/SU drifts to 50% by "
                        "construction. Read Over/Under for totals; for sides add H, D, F, etc.")
    if 0 < len(played) < 100:
        warnings.append(f"Small sample ({len(played)} rows): a record like this can happen by chance.")

    out = {
        "query": text, "sport": sport, "interpreted": q.interpreted, "sql_where": q.where,
        "rows": len(played), "games": len({r["game_id"] for r in played}),
        "all": _summarise(played, teasers=True),
        "by_season": [{"season": s, **_summarise(by_season[s])} for s in seasons],
        "warnings": warnings,
        "recent": [_row_brief(r) for r in (played[-limit_games:] if limit_games else [])][::-1],
        "upcoming": [_row_brief(r) for r in upcoming],
    }
    if test_from:
        train = [r for r in played if r["season"] < test_from]
        test = [r for r in played if r["season"] >= test_from]
        out["split"] = {"test_from": test_from, "train": _summarise(train), "test": _summarise(test)}
    return out


def _row_brief(r) -> dict:
    keys = ("game_id", "date", "season", "week", "team", "opp", "home", "neutral", "pts", "opp_pts", "margin",
            "line", "total_line", "ml", "spread_odds", "over_odds", "under_odds", "hour", "covered", "ats_push", "ats_margin", "over", "ou_margin", "total",
            "won", "lost", "tie", "wind", "wind_fcst", "temp", "rank", "opp_rank", "team_name", "opp_name")
    return {k: r[k] for k in keys}


def fmt_record(t: dict, label: str) -> str:
    if not t["n"] and not t["p"]:
        return f"{label:<8} —"
    pct = f"{t['pct']:.1%}" if t["pct"] is not None else "—"
    if label == "SU":  # straight-up record has no price attached
        return f"{label:<8} {t['w']}-{t['l']}-{t['p']}  {pct:>6}"
    roi = f"{t['roi']:+.1%}" if t["roi"] is not None else "—"
    return (f"{label:<8} {t['w']}-{t['l']}-{t['p']}  {pct:>6}  units {t['units']:+.1f}  "
            f"ROI {roi:>7}  z={t['z']:+.2f}  p={t['p_value']}")


def format_text(res: dict) -> str:
    a = res["all"]
    lines = [f"Query [{res['sport'].upper()}]: {res['query']}", f"Interpreted as: {res['interpreted']}",
             f"Games: {res['games']}  (team rows: {res['rows']})", ""]
    lines += [fmt_record(a["su"], "SU"), fmt_record(a["ats"], "ATS"), fmt_record(a["ml"], "ML"),
              fmt_record(a["over"], "Over"), fmt_record(a["under"], "Under")]
    t = a.get("teasers") or []
    if t:
        lines += ["", "Teasers:      " + "".join(f"{x['points']:+d} pt".rjust(12) for x in t)]
        for key, label in (("ats", "ATS"), ("over", "Over"), ("under", "Under")):
            lines.append(f"  {label:<11} " + "".join(f"{x[key]['w']}-{x[key]['l']}-{x[key]['p']}".rjust(12) for x in t))
    if "split" in res:
        s = res["split"]
        lines += ["", f"Out of sample (before {s['test_from']} vs since {s['test_from']}):"]
        for k in ("ats", "under", "over"):
            lines.append(f"  {k.upper():<6} before: {_short(s['train'][k])}   |   since: {_short(s['test'][k])}")
    lines += ["", "By season (ATS | Over/Under):"]
    for s in res["by_season"]:
        lines.append(f"  {s['season']}: ATS {_short(s['ats'])}   O/U {s['over']['w']}-{s['under']['w']}-{s['over']['p']}")
    if res["upcoming"]:
        lines += ["", "Upcoming games that match:"]
        for r in res["upcoming"]:
            lines.append(f"  {r['date']} {r['team']} vs {r['opp']}  line {r['line']}  total {r['total_line']}"
                         f"  forecast wind {r['wind_fcst']}")
    for w in res["warnings"]:
        lines.append("⚠ " + w)
    return "\n".join(lines)


def _short(t: dict) -> str:
    if not t["n"]:
        return "—"
    return f"{t['w']}-{t['l']}-{t['p']} ({t['pct']:.0%}, ROI {t['roi']:+.0%})"
